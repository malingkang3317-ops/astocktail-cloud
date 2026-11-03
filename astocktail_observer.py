#!/usr/bin/env python3
"""AStockTail bounded rank-list observer. Research signals only, no order placement.
Python 3.11+, installed eltdx. No AKShare, pandas or old strategy.py dependency.
"""
from __future__ import annotations
import argparse
import csv
import fcntl
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
VERSION = 'rank-observer-1.0.0'
GROUPS = ('C', 'C_MA5_GT_MA10', 'B_MA5_GT_MA10')
RULES = {
    'cutoff': '14:49', 'change_pct': [3,5], 'volume_ratio_gt': 1,
    'turnover_pct': [5,10], 'float_cap_yuan': [5e9,2e10],
    'vwap_above_fraction': .70, 'tail_volume': 'median5 > (median3+median4)/2',
    'volume_segments': '200 trading minutes from 10:00, five groups of 40',
    'index_outperformance_gt': 0, 'setup_window': ['14:40','14:49'],
    'B': 'high > prior high since 14:00',
    'C': 'high >= 0.995 * prior high since 13:01',
    'pullback': 'later complete minute drops >=0.0005; all lows from setup+1 through pullback >= VWAP',
    'MA': 'prior completed closes plus 14:49 price, MA5 > MA10',
    'volume_ratio': '(volume_lots/229)/(mean(prior5 volume_lots)/240)',
    'preselection_band': [2.5,5.5], 'preselection_times': ['14:40','14:45','14:49'],
    'preselection_relaxed': {'volume_ratio_gt': .8, 'turnover_pct':[3,12], 'float_cap_yuan':[4.5e9,2.2e10]},
    'rank_page_size':80, 'rank_max_pages':20, 'rank_seconds':15,
    'max_prepare':300, 'max_technical':100, 'push_max':3,
    'order': 'preliminary volume ratio descending, turnover descending, code ascending; final push uses final same fields',
    'hard_cutoff':'14:55', 'final_fetch_cutoff':'14:54:30',
}
SHAPES = {'B':'short breakout','C':'near afternoon high'}
CONFIG_HASH = hashlib.sha256(json.dumps(RULES,sort_keys=True).encode()).hexdigest()
EXPECTED = [f'{h:02d}:{m:02d}' for h,a,z in ((9,31,59),(10,0,59),(11,0,30),(13,1,59),(14,0,49)) for m in range(a,z+1)]


def now(): return datetime.now(TZ)
def stamp(): return now().isoformat(timespec='milliseconds')
def log(message): print(f'[{stamp()}] {message}',flush=True)
def at(day, hm): return datetime.fromisoformat(f'{day}T{hm}:00').replace(tzinfo=TZ)
def parse_time(value):
    dt = datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
def number(value):
    n = float(value)
    if not math.isfinite(n): raise ValueError('nonfinite_number')
    return n


def write_json(path, value, immutable=False):
    path = Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    content = json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)
    if immutable:
        with path.open('x',encoding='utf-8') as f: f.write(content)
    else:
        tmp = path.with_name(path.name+f'.{os.getpid()}.tmp')
        tmp.write_text(content,encoding='utf-8'); tmp.replace(path)


def rows_of(value, keys=('records','rows','bars')):
    if isinstance(value,list): return value
    if isinstance(value,dict):
        for key in keys:
            if isinstance(value.get(key),list): return value[key]
    raise ValueError('unsupported_response_shape')


def code_of(row):
    code = str(row.get('full_code') or '').lower()
    if not code:
        c = str(row.get('code') or '').zfill(6)
        exchange = str(row.get('exchange') or '').lower()
        if exchange not in ('sh','sz'):
            exchange = 'sh' if c.startswith(('60','68')) else 'sz' if c.startswith(('00','30')) else ''
        code = exchange+c
    return code if re.fullmatch(r'(sh(?:60|68)|sz(?:00|30))\d{4}',code) else None


def quote_values(row):
    price, prev = number(row['last_price']),number(row['pre_close_price'])
    if min(price,prev)<=0: raise ValueError('invalid_quote_price')
    return price,prev,100*(price/prev-1)


def daily_map(raw):
    result = {}
    for b in raw:
        day = parse_time(b['time']).date().isoformat()
        vals = {k:number(b[k]) for k in ('open','high','low','close')}
        if min(vals.values())<=0 or vals['high'] < max(vals.values()) or vals['low'] > min(vals.values()):
            raise ValueError('invalid_daily_ohlc')
        vol = number(b.get('volume_lots',0))
        if vol<0 or day in result: raise ValueError('invalid_or_duplicate_daily_bar')
        result[day] = {**vals,'volume_lots':vol}
    return result


def _network_worker(pipe):
    """One client per worker; parent can kill any stalled native/network call."""
    try:
        from eltdx import TdxClient,to_jsonable
        with TdxClient(timeout=5) as client:
            while True:
                request = pipe.recv()
                if request is None: return
                op,kw = request
                try:
                    if op=='rank': value=client.quotes.list_by_category(**kw)
                    elif op=='profile': value=client.helpers.stock_profile_table(**kw)
                    elif op=='bars':
                        code=kw.pop('code')
                        value=client.bars.get(code,**kw)
                    elif op=='version':
                        from importlib.metadata import version
                        value={'eltdx':version('eltdx')}
                    else: raise ValueError('unknown_operation')
                    pipe.send({'ok':True,'data':to_jsonable(value)})
                except Exception as exc:
                    pipe.send({'ok':False,'error':type(exc).__name__})
    except (EOFError,BrokenPipeError): pass
    except Exception as exc:
        try: pipe.send({'ok':False,'error':type(exc).__name__})
        except Exception: pass
    finally: pipe.close()


class FeedError(RuntimeError):
    """Remote exception class only; never carries URLs or credentials."""


class Market:
    def __init__(self, audit_dir=None):
        self.audit_dir=Path(audit_dir) if audit_dir else None
        self.ctx=mp.get_context('spawn'); self.proc=None; self.pipe=None
        self.counter=0; self.events=[]
    def close(self):
        if self.proc is not None:
            if self.proc.is_alive(): self.proc.terminate()
            self.proc.join(1)
            if self.proc.is_alive(): self.proc.kill(); self.proc.join(1)
            self.proc=None
        if self.pipe is not None: self.pipe.close(); self.pipe=None
    def call(self, op, deadline, **kw):
        remaining=deadline-time.monotonic()
        if remaining<=0: raise TimeoutError('stage_deadline')
        t=time.monotonic(); started=stamp(); self.counter+=1
        event={'op':op,'request':kw,'started_at':started}
        try:
            if self.proc is None:
                self.pipe,child=self.ctx.Pipe()
                self.proc=self.ctx.Process(target=_network_worker,args=(child,),daemon=True)
                self.proc.start(); child.close()
            self.pipe.send((op,kw))
            if not self.pipe.poll(min(12,remaining)):
                self.close(); raise TimeoutError('request_timeout')
            reply=self.pipe.recv()
            if not reply.get('ok'):
                event['remote_error']=reply.get('error','network_error')
                raise FeedError(event['remote_error'])
            value=reply['data']; event.update(status='ok',data=value)
            return value
        except Exception as exc:
            self.close(); event.update(status='error',error=type(exc).__name__)
            raise
        finally:
            event.update(finished_at=stamp(),seconds=round(time.monotonic()-t,4))
            self.events.append({k:v for k,v in event.items() if k!='data'})
            if self.audit_dir:
                write_json(self.audit_dir/f'{self.counter:04d}_{op}.json',event,immutable=True)


def deadline_for(day, hm): return time.monotonic()+max(0,(at(day,hm)-now()).total_seconds())
def wait_until(target):
    while now()<target: time.sleep(min(20,(target-now()).total_seconds()))


def scan_ranks(market, deadline):
    started=time.monotonic(); stop=min(deadline,started+RULES['rank_seconds'])
    found={}; unique=set(); stats={'pages':0,'rows':0,'invalid':0,'duplicates':0,
                                  'cross_page_reordering':False,'stop_reason':'page_limit'}
    previous_last=None
    for page in range(RULES['rank_max_pages']):
        if time.monotonic()>=stop: stats['stop_reason']='time_limit'; break
        try:
            raw=market.call('rank',stop,category='沪深A股',sort_by='涨幅',
                            start=page*RULES['rank_page_size'],count=RULES['rank_page_size'],ascending=False)
            rows=rows_of(raw,('records',)); stats['pages']+=1; stats['rows']+=len(rows)
            if not rows: stats['stop_reason']='empty_page'; break
            changes=[]
            for rank,row in enumerate(rows):
                try:
                    code=code_of(row)
                    if code is None: raise ValueError('not_sh_sz_stock')
                    price,prev,change=quote_values(row); changes.append(change)
                    if code in unique: stats['duplicates']+=1
                    unique.add(code)
                    if 2.5<=change<=5.5:
                        found[code]={'code':code,'quote':row,'change_pct':change,
                                     'rank':page*RULES['rank_page_size']+rank,'observed_at':stamp()}
                except (ValueError,KeyError,TypeError): stats['invalid']+=1
            if any(a+1e-7<b for a,b in zip(changes,changes[1:])):
                stats['stop_reason']='invalid_sort_order'; break
            if changes:
                if previous_last is not None and changes[0]>previous_last+1e-7:
                    stats['cross_page_reordering']=True
                previous_last=changes[-1]
                # Early stop only for a wholly valid ordered page below the band.
                if len(changes)==len(rows) and changes[-1]<2.5:
                    stats['stop_reason']='below_band'; break
            if len(rows)<RULES['rank_page_size']: stats['stop_reason']='end_of_list'; break
        except Exception as exc:
            stats.update(stop_reason='request_error',error=type(exc).__name__); break
    stats.update(unique_seen=len(unique),band_candidates=len(found),seconds=round(time.monotonic()-started,4))
    return sorted(found.values(),key=lambda r:(r['rank'],r['code'])),stats


def pre_metrics(item, profile, days, target_day):
    price,prev,change=quote_values(item['quote'])
    shares=number(profile['circulating_shares'])
    vol=number(item['quote']['total_hand'])
    if shares<=0 or vol<0: raise ValueError('invalid_shares_or_volume')
    prior=[b for d,b in sorted(days.items()) if d<target_day and b['volume_lots']>0][-5:]
    if len(prior)!=5: raise ValueError('insufficient_daily_volume_history')
    observed=parse_time(item['observed_at']).strftime('%H:%M')
    session=EXPECTED+[f'14:{m:02d}' for m in range(50,60)]+['15:00']
    elapsed=max(1,sum(t<=observed for t in session))
    ratio=(vol/elapsed)/(statistics.mean(b['volume_lots'] for b in prior)/240)
    turnover=vol*100/shares*100; cap=price*shares
    return {'volume_ratio':ratio,'turnover_pct':turnover,'float_cap_yuan':cap,
            'eligible':ratio>.8 and 3<=turnover<=12 and 4.5e9<=cap<=2.2e10}


def order_key(row):
    m=row.get('basic',row.get('pre',{}))
    return (-m.get('volume_ratio',0),-m.get('turnover_pct',0),row['code'])


def prepare_pool(market, hits, pool, profiles, histories, day, deadline):
    stats={'rank_band':len(hits),'prepared':0,'basic_rejected':0,'missing':0,'capacity_skipped':0,'errors':[]}
    # Bound preparation as well as expensive minute analysis.
    hits=sorted(hits,key=lambda r:(r['rank'],r['code']))
    pending=[r['code'] for r in hits if r['code'] not in profiles][:RULES['max_prepare']]
    for offset in range(0,len(pending),40):
        if time.monotonic()>=deadline: break
        try:
            rows=rows_of(market.call('profile',deadline,codes=pending[offset:offset+40]),('rows',))
            for row in rows:
                code=code_of(row)
                if code in pending:
                    shares=number(row.get('circulating_shares',0))
                    if shares>0: profiles[code]={'data':row,'collected_at':stamp()}
        except Exception as exc:
            stats['errors'].append({'stage':'profile','error':type(exc).__name__})
    for item in hits:
        code=item['code']
        if len(histories)>=RULES['max_prepare'] and code not in histories:
            stats['capacity_skipped']+=1; continue
        if time.monotonic()>=deadline:
            stats['missing']+=1; continue
        try:
            if code not in profiles: raise ValueError('missing_profile')
            if code not in histories:
                raw=market.call('bars',deadline,code=code,period='day',count=40)
                histories[code]=daily_map(rows_of(raw,('bars',)))
            metrics=pre_metrics(item,profiles[code]['data'],histories[code],day)
            stats['prepared']+=1
            if metrics['eligible']:
                pool[code]={**item,'pre':metrics,'name':profiles[code]['data'].get('name',code)}
            else:
                pool.pop(code,None); stats['basic_rejected']+=1
        except Exception as exc:
            stats['missing']+=1
            if len(stats['errors'])<20: stats['errors'].append({'code':code,'error':type(exc).__name__})
    stats['pool_size']=len(pool)
    return stats
def index_for(code):
    if code.startswith('sz30'): return 'sz399006'
    if code.startswith('sh68'): return 'sh000688'
    return 'sh000001' if code.startswith('sh') else 'sz399001'


def minute_points(raw, day):
    points=[]
    for b in raw:
        try:
            ts=parse_time(b['time'])
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError('缓存时间戳非法') from exc
        tm=ts.strftime('%H:%M')
        if ts.date().isoformat()!=day or tm>'14:49': continue
        if not ('09:31'<=tm<='11:30' or '13:01'<=tm<='14:49'): continue
        p={k:float(b.get(k) or 0) for k in ('close','high','low','amount','volume_lots')}
        if not all(math.isfinite(x) for x in p.values()): raise ValueError('分钟值含NaN/inf')
        if p['close']<=0 or p['high']<p['low'] or p['low']<=0 or p['amount']<0 or p['volume_lots']<0:
            raise ValueError(f'{tm}分钟价格或成交量异常')
        points.append({'time':tm,'price':p['close'],'high':p['high'],'low':p['low'],
                       'amount':p['amount'],'volume':p['volume_lots']})
    points.sort(key=lambda p:p['time'])
    expected=[]
    for h,a,z in ((9,31,59),(10,0,59),(11,0,30),(13,1,59),(14,0,49)):
        expected.extend(f'{h:02d}:{m:02d}' for m in range(a,z+1))
    if [p['time'] for p in points]!=expected:
        raise ValueError(f'{day}分钟数据不完整或重复: 得到{len(points)}/{len(expected)}')
    total_amount=total_shares=0.0
    for p in points:
        total_amount+=p['amount'];total_shares+=p['volume']*100
        if total_shares<=0: raise ValueError('累计成交量为0，无法生成VWAP')
        p['vwap']=total_amount/total_shares
    return points


def index_series(raw,day):
    idx={}
    for b in raw:
        ts=parse_time(b['time'])
        tm=ts.strftime('%H:%M')
        if ts.date().isoformat()==day and tm<='14:49':
            price=float(b.get('close') or 0)
            if not math.isfinite(price) or price<=0:raise ValueError('指数价格不合法')
            if tm in idx:raise ValueError('指数分钟时间重复')
            idx[tm]=price
    return idx


def condition_flags(points,indices):
    matched=[(p,indices[p['time']]) for p in points if p['time'] in indices]
    if len(matched)<220: raise ValueError(f'指数有效分钟对齐不足:{len(matched)}')
    rel=100*((matched[-1][0]['price']/matched[0][0]['price']-1)-
              (matched[-1][1]/matched[0][1]-1))
    above=sum(p['price']>=p['vwap'] for p in points)/len(points)
    seq=[p for p in points if p['time']>='10:00']
    if len(seq)!=200: raise ValueError(f'10:00后有效分钟数错误:{len(seq)}')
    medians=[statistics.median(p['volume'] for p in seq[i:i+40]) for i in range(0,200,40)]
    baseline=(medians[2]+medians[3])/2
    if baseline<=0: raise ValueError('第3、4段成交量中位数基准为0')
    return ({'volume_tail_above_prior_baseline':medians[4]>baseline,
             'stronger_than_index':rel>0,
             'vwap_above_70pct':above>=0.70},
            {'volume_medians':medians,'tail_over_baseline':round(medians[4]/baseline,5),
             'outperformance_pct_points':round(rel,5),'above_vwap_fraction':round(above,5)})


def shape_events(points,variant):
    """Only historical highs known BEFORE a given 1-minute bar may be used.
    Breakout/near-high is observed using that completed bar high. The pullback
    must occur in a strictly later completed minute and stay above its VWAP;
    every intervening minute's low through the pullback must respect VWAP.
    A/B: true high breakout; C: >=99.5% of prior afternoon high (includes breakouts).
    All variants use >=0.05% pullback on a later bar for comparability.
    """
    if variant not in SHAPES:raise ValueError(variant)
    base_start='14:00' if variant=='B' else '13:01'
    running=-float('inf')
    candidates=[]
    for i,p in enumerate(points):
        tm=p['time']
        if tm < base_start:continue
        # Compare against earlier bars only, never include the current high in baseline.
        if '14:40'<=tm<='14:49' and running>0:
            if variant=='C': qualify=p['high']>=running*0.995
            else: qualify=p['high']>running
            if qualify:candidates.append((i,running))
        running=max(running,p['high'])
    event=None
    for i,prior_high in candidates:
        peak=points[i]['high']
        for j in range(i+1,len(points)):
            low=points[j]['low']
            drop=(peak-low)/peak
            if drop>=0.0005 and all(p['low']>=p['vwap'] for p in points[i+1:j+1]):
                event={'setup_minute':points[i]['time'],'pullback_minute':points[j]['time'],
                       'setup_high':peak,'prior_high':prior_high,'drop_pct':round(drop*100,5),
                       'pullback_low':low,'pullback_vwap':round(points[j]['vwap'],5)}
                break
        if event is not None:break
    return {'setup':bool(candidates),'pullback':event is not None,'event':event,
            'setup_minutes':[points[i]['time'] for i,_ in candidates]}




def evaluate_candidate(item, daily, profile, stock_raw, index_raw, day, calendar):
    points=minute_points(stock_raw,day)
    index=index_series(index_raw,day)
    if any(t not in index for t in EXPECTED): raise ValueError('incomplete_index_through_1449')
    # Stronger data validation does not silently weaken original conditions.
    for p in points:
        if not p['low']<=p['price']<=p['high']: raise ValueError('invalid_minute_ohlc')
    history=[(d,b) for d,b in sorted(daily.items()) if d<day]
    if not history: raise ValueError('missing_previous_close')
    prior_five=[(d,b) for d,b in history if b['volume_lots']>0][-5:]
    expected_five=[d for d in sorted(calendar) if d<day][-5:]
    if len(prior_five)!=5 or [d for d,b in prior_five]!=expected_five:
        raise ValueError('missing_prior_five_market_sessions')
    prev=history[-1][1]['close']; price=points[-1]['price']
    shares=number(profile['data']['circulating_shares'])
    if shares<=0: raise ValueError('invalid_circulating_shares')
    # Current quote previous-close mismatch may indicate an ex-rights or stale bar.
    try: quote_prev=number(item['quote']['pre_close_price'])
    except (ValueError,KeyError,TypeError): raise ValueError('missing_quote_previous_close')
    if abs(quote_prev-prev)>max(.011,prev*.001):
        raise ValueError('daily_quote_previous_close_mismatch')
    volume=sum(p['volume'] for p in points)
    ratio=(volume/len(points))/(statistics.mean(b['volume_lots'] for d,b in prior_five)/240)
    change=(price/prev-1)*100; turnover=volume*100/shares*100; cap=price*shares
    basic_flags={'change_3_5':3<=change<=5,'volume_ratio_gt_1':ratio>1,
                 'turnover_5_10':5<=turnover<=10,'float_cap_50_200yi':5e9<=cap<=2e10}
    flags,metrics=condition_flags(points,index)
    variants={v:shape_events(points,v) for v in SHAPES}
    common=all(basic_flags.values()) and all(flags.values())
    closes=[b['close'] for d,b in history]
    trend_available=len(closes)>=20
    ma5=(sum(closes[-4:])+price)/5 if len(closes)>=4 else None
    ma10=(sum(closes[-9:])+price)/10 if len(closes)>=9 else None
    trend=ma5>ma10 if trend_available else None
    B=common and variants['B']['pullback']; C=common and variants['C']['pullback']
    return {'date':day,'code':item['code'],'name':item['name'],'signal_price_1449':price,
            'basic':{'price':price,'change_pct':change,'volume_ratio':ratio,'turnover_pct':turnover,
                     'float_cap_yuan':cap,'volume_lots':volume,'previous_close':prev},
            'basic_flags':basic_flags,'common_flags':flags,'common_metrics':metrics,'variants':variants,
            'ma5':ma5,'ma10':ma10,'trend_available':trend_available,'ma5_gt_ma10':trend,
            'groups':{'C':C,'C_MA5_GT_MA10':C and trend is True,'B_MA5_GT_MA10':B and trend is True},
            'share_source':{'collected_at':profile['collected_at'],
                            'updated_date':profile['data'].get('updated_date'),
                            'circulating_shares':shares},
            'evaluated_at':stamp(),'data_cutoff':f'{day}T14:49:00+08:00'}


def final_evaluate(market, pool, profiles, histories, day, calendar, deadline):
    chosen=sorted(pool.values(),key=order_key)[:RULES['max_technical']]
    stats={'pool_size':len(pool),'attempted':0,'evaluated':0,
           'capacity_skipped':max(0,len(pool)-len(chosen)),'timeout_skipped':0,'data_errors':[],
           'retries':0,'selected_for_analysis':[r['code'] for r in chosen]}
    results=[]; indices={}; pending=chosen
    # At most two passes; a missing 14:49 minute is not converted to a false signal.
    for attempt in range(2):
        again=[]
        for item in pending:
            if time.monotonic()>=deadline:
                stats['timeout_skipped']+=1; continue
            stats['attempted']+=1
            code=item['code']; idx=index_for(code)
            try:
                if idx not in indices:
                    raw=rows_of(market.call('bars',deadline,code=idx,period='1m',count=240,kind='index'),('bars',))
                    series=index_series(raw,day)
                    if any(t not in series for t in EXPECTED): raise ValueError('incomplete_index')
                    indices[idx]=raw
                raw=rows_of(market.call('bars',deadline,code=code,period='1m',count=240),('bars',))
                result=evaluate_candidate(item,histories[code],profiles[code],raw,indices[idx],day,calendar)
                results.append(result); stats['evaluated']+=1
            except Exception as exc:
                if attempt==0 and isinstance(exc,(TimeoutError,RuntimeError,EOFError,ValueError)):
                    again.append(item)
                else:
                    stats['data_errors'].append({'code':code,'error':type(exc).__name__,
                                                'reason':str(exc)[:120] if isinstance(exc,ValueError) else 'request_failed'})
        pending=again
        if not pending: break
        if attempt==0:
            stats['retries']=len(pending)
            time.sleep(min(1,max(0,deadline-time.monotonic())))
    return results,stats


def stats_values(values):
    good=[number(v) for v in values if v is not None]
    return {'n':len(good),'mean_pct':statistics.mean(good) if good else None,
            'median_pct':statistics.median(good) if good else None,
            'win_rate_pct':100*sum(v>0 for v in good)/len(good) if good else None,
            'worst_pct':min(good) if good else None}


def notification_text(report):
    title=f"A股尾盘观察 {report['date']} {report['status']}"
    text=[title,f"数据截止：14:49；生成：{report['finished_at']}",
          f"技术复核：{report.get('analysis',{}).get('evaluated',0)}只；有限扫描，允许漏选"]
    analysis=report.get('analysis',{})
    text.append(f"预选池{analysis.get('pool_size',0)}只；容量跳过{analysis.get('capacity_skipped',0)}只；"
                f"超时跳过{analysis.get('timeout_skipped',0)}只；最终数据失败{len(analysis.get('data_errors',[]))}只")
    if report.get('error'): text.append('运行问题：'+report['error'].get('reason',report['error']['type']))
    if report['status']!='timed_out' and report.get('mode')=='live':
        for row in report.get('pushed',[]):
            text.append(f"{row['code']} {row['name']} 参考价{row['signal_price_1449']:.2f} 涨幅{row['basic']['change_pct']:.2f}% 量比{row['basic']['volume_ratio']:.2f}")
        if not report.get('pushed'): text.append('主要观察组本次没有可推送候选。')
    else: text.append('超时或演练：不推送尾盘候选。')
    text.append('三组信号数：'+str(report.get('group_counts',{})))
    text.append('14:49是研究参考价，不是实际成交价；未验证手机送达。')
    return title,'\n'.join(text)


def _notify_worker(pipe,key,title,body):
    try:
        request=urllib.request.Request('https://sctapi.ftqq.com/'+key+'.send',
            data=urllib.parse.urlencode({'title':title,'desp':body}).encode(),method='POST')
        with urllib.request.urlopen(request,timeout=7) as response:
            data=json.loads(response.read(100000))
        code=data.get('code')
        ok=(code==0) or (code is None and data.get('data',{}).get('errno')==0)
        pipe.send({'status':'accepted_by_service' if ok else 'rejected_by_service'})
    except Exception as exc:
        pipe.send({'status':'error','error':type(exc).__name__})
    finally: pipe.close()


def send_notification(report, enabled, directory, live=True):
    token=hashlib.sha256(report['started_at'].encode()).hexdigest()[:12]
    dest=directory/f'notification_{token}.json'
    if dest.exists(): return
    if not enabled or not live:
        result={'status':'disabled'}
    elif not os.environ.get('SERVERCHAN_SENDKEY','').strip(): result={'status':'missing_SERVERCHAN_SENDKEY'}
    else:
        # After the deadline, send status only even if the signal calculation finished earlier.
        copy=dict(report)
        if now()>=at(report['date'],'14:55'): copy.update(status='timed_out',pushed=[])
        title,body=notification_text(copy)
        ctx=mp.get_context('spawn'); parent,child=ctx.Pipe()
        proc=ctx.Process(target=_notify_worker,args=(child,os.environ['SERVERCHAN_SENDKEY'].strip(),title,body),daemon=True)
        started=time.monotonic(); proc.start(); child.close()
        try:
            result=parent.recv() if parent.poll(8) else {'status':'timeout_delivery_unknown'}
        except (EOFError,OSError): result={'status':'worker_error_delivery_unknown'}
        finally:
            proc.join(.1)
            if proc.is_alive(): proc.terminate(); proc.join(1)
            if proc.is_alive(): proc.kill(); proc.join(1)
            parent.close()
        result['seconds']=round(time.monotonic()-started,3)
    result.update(recorded_at=stamp(),actual_device_delivery='not_measured',
                  planned_codes=[r['code'] for r in report.get('pushed',[])])
    write_json(dest,result,immutable=True)
    log('通知状态：'+result['status'])


def market_calendar(market,deadline):
    raw=rows_of(market.call('bars',deadline,code='sh000001',period='day',count=160,kind='index'),('bars',))
    days=daily_map(raw)
    if not days: raise ValueError('empty_market_calendar')
    return days


def run_observation(root, mode, notify=False):
    live=mode=='live'; today=now().date().isoformat()
    base=root/'observer_data'; base.mkdir(parents=True,exist_ok=True)
    lock=(base/('live.lock' if live else 'rehearsal.lock')).open('a')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        log('已有同模式任务正在运行，本次退出'); lock.close(); return 2
    runid=now().strftime('%Y%m%d_%H%M%S_%f')
    directory=base/'live'/today if live else base/'rehearsal'/runid
    directory.mkdir(parents=True,exist_ok=True)
    if live and (directory/'signals.json').exists():
        log('今日信号已冻结，不重算、不重复推送'); lock.close(); return 0
    market=Market(directory/'raw'/runid)
    report={'version':VERSION,'rules_hash':CONFIG_HASH,'code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'rules':RULES,'mode':mode,'date':today,'started_at':stamp(),
            'status':'starting','scans':[],'rows':[],'pushed':[],
            'scope':'bounded_rank_pool_not_full_market',
            'limitations':['当前披露流通股本，不保证等于交易所逐日口径',
                          '原始未复权价格收益；除权除息需另外核查',
                          '仅分钟OHLC形态代理，非成交回放；未扣成本',
                          '榜单分页时间不同且允许漏选，非全市场同一时刻快照']}
    exitcode=0
    try:
        if live:
            if now()<at(today,'14:35') or now()>=at(today,'14:50'):
                # No misleading intraday candidates when manually started too late.
                report['status']='start_outside_window'
                raise ValueError('live_start_must_be_1435_to_before_1450')
            if now().weekday()>=5:
                report['status']='closed_weekend'; return 0
        calendar=market_calendar(market,time.monotonic()+20)
        if live and today not in calendar:
            report['status']='no_current_session_data'; return 2
        day=today if live else max(calendar)
        report['date']=day
        if not live and day==today and now()<at(today,'15:10'):
            raise ValueError('rehearse_requires_after_1510_or_closed_day')
        pool={}; profiles={}; histories={}
        stages=RULES['preselection_times'] if live else ['rehearsal_now']
        for stage in stages:
            if live:
                target=at(day,stage)
                # Skip stages already missed by >45sec, but allow a late final preselection.
                if now()>target+timedelta(seconds=45):
                    report['scans'].append({'stage':stage,'status':'missed_start'}); continue
                wait_until(target)
                stop=min(time.monotonic()+(120 if stage!='14:49' else 45),deadline_for(day,'14:49')+50)
            else: stop=time.monotonic()+120
            log(f'涨幅榜扫描 {stage}')
            hits,scan=scan_ranks(market,stop)
            prepared=prepare_pool(market,hits,pool,profiles,histories,day,stop)
            report['scans'].append({'stage':stage,'rank':scan,'prepare':prepared})
            write_json(directory/'progress.json',report)
            log(f"榜单{scan['unique_seen']}只，区间{len(hits)}只，预选池{len(pool)}只，停止原因{scan['stop_reason']}")
        if not report['scans'] or not any(s.get('rank',{}).get('unique_seen',0) for s in report['scans']):
            raise ValueError('no_valid_rank_scan')
        if live: wait_until(at(day,'14:50'))
        deadline=deadline_for(day,'14:54')+30 if live else time.monotonic()+180
        # Absolute deadline calculation, including when start already passed.
        if live: deadline=time.monotonic()+max(0,(at(day,'14:54')+timedelta(seconds=30)-now()).total_seconds())
        results,analysis=final_evaluate(market,pool,profiles,histories,day,calendar,deadline)
        report.update(rows=results,analysis=analysis)
        write_json(directory/'prepared_inputs.json',{'profiles':profiles,'daily':histories,'pool':pool},immutable=True)
        analysis['trend_missing']=sum(not r['trend_available'] for r in results)
        report['condition_pass_counts']={'denominator_evaluated':len(results)}
        for category in ('basic_flags','common_flags'):
            report['condition_pass_counts'][category]={k:sum(r[category][k] for r in results)
                for k in (results[0][category] if results else {})}
        report['condition_pass_counts']['shape_pullback']={v:sum(r['variants'][v]['pullback'] for r in results) for v in SHAPES}
        report['condition_pass_counts']['ma5_gt_ma10']=sum(r['ma5_gt_ma10'] is True for r in results)
        errors=bool(analysis['data_errors'] or analysis['timeout_skipped'] or analysis['trend_missing'])
        errors=errors or any(s.get('rank',{}).get('stop_reason') in ('request_error','invalid_sort_order') or s.get('prepare',{}).get('missing',0) for s in report['scans'])
        report['status']='partial' if errors else 'ok'
        if live and now()>=at(day,'14:55'): report['status']='timed_out'
        report['group_counts']={g:sum(r['groups'][g] for r in results) for g in GROUPS}
        if live and report['status']!='timed_out':
            report['pushed']=sorted([r for r in results if r['groups']['C_MA5_GT_MA10']],key=order_key)[:3]
        if not live: report['status']='rehearsal_'+report['status']
        log(f"复核完成 {analysis['evaluated']}只，三组：{report['group_counts']}")
    except Exception as exc:
        if report['status']=='starting': report['status']='data_error'
        elif report['status'] not in ('start_outside_window',): report['status']='data_error'
        report['error']={'type':type(exc).__name__,'reason':str(exc)[:120] if isinstance(exc,(ValueError,FeedError)) else 'request_or_runtime_error'}
        report['pushed']=[]; exitcode=2
        log('运行未完成：'+report['error']['type'])
    finally:
        market.close(); report['finished_at']=stamp(); report['request_timings']=market.events
        # Bad starts/closed sessions do not freeze a real signal day; can restart correctly later.
        name='signals.json' if report['status'] not in ('start_outside_window','closed_weekend','no_current_session_data','data_error') else f'status_{runid}.json'
        write_json(directory/name,report,immutable=True)
        if name=='signals.json' or report['status'] in ('data_error','no_current_session_data'):
            send_notification(report,notify,directory,live=live)
        lock.close(); log('结果：'+str(directory/name))
    return exitcode

def outcome_for(signal, daily, calendar, horizon):
    day=signal['date']; calendar=sorted(calendar)
    if day not in calendar: return {'status':'calendar_missing','return_pct':None}
    pos=calendar.index(day)+horizon
    if pos>=len(calendar): return {'status':'pending','return_pct':None}
    target=calendar[pos]; sessions=calendar[pos-horizon+1:pos+1]
    if any(d not in daily for d in sessions):
        return {'status':'missing_session_or_suspended','target_day':target,'return_pct':None}
    previous=[d for d in daily if d<day]
    if not previous: return {'status':'price_basis_unverified','return_pct':None}
    recorded=signal['basic']['previous_close']; current=daily[max(previous)]['close']
    if abs(recorded-current)>max(.011,recorded*.001):
        return {'status':'historical_price_basis_changed','return_pct':None}
    entry=signal['signal_price_1449']; end=daily[target]
    return {'status':'available','target_day':target,'return_pct':100*(end['close']/entry-1),
            'target_open_pct':100*(end['open']/entry-1),
            'lowest_pct':100*(min(daily[d]['low'] for d in sessions)/entry-1),
            'highest_pct':100*(max(daily[d]['high'] for d in sessions)/entry-1),
            'price_basis':'unadjusted_gross_reference_not_executable_return'}


def summary_tables(root, outcomes):
    reports=[]
    for path in sorted((root/'observer_data/live').glob('*/signals.json')):
        reports.append(json.loads(path.read_text(encoding='utf-8')))
    allrows=[]
    for report in reports:
        planned={r['code'] for r in report.get('pushed',[])}
        week=date.fromisoformat(report['date']).isocalendar()
        for row in report.get('rows',[]):
            allrows.append((row,f'{week.year}-W{week.week:02d}',row['code'] in planned))
    periods=['ALL']+sorted({w for r,w,p in allrows})
    records=[]
    for period in periods:
        for group in GROUPS:
            for population in ('all_signals','planned_push'):
                chosen=[r for r,w,p in allrows if (period=='ALL' or w==period) and
                        r['groups'][group] and (population=='all_signals' or p)]
                for horizon in (1,3,5):
                    known=[]; lows=[]
                    for row in chosen:
                        value=outcomes.get(row['date']+'|'+row['code'],{}).get('horizons',{}).get(str(horizon),{})
                        if value.get('status')=='available':
                            known.append(value['return_pct']); lows.append(value['lowest_pct'])
                    records.append({'period':period,'group':group,'population':population,'horizon':horizon,
                                    'signal_n':len(chosen),'missing_or_pending':len(chosen)-len(known),
                                    **stats_values(known),'worst_low_pct':min(lows) if lows else None})
    path=root/'observer_data/outcomes/summary.csv'; path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    columns=['period','group','population','horizon','signal_n','missing_or_pending','n','mean_pct','median_pct','win_rate_pct','worst_pct','worst_low_pct']
    with temp.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns); writer.writeheader(); writer.writerows(records)
    temp.replace(path)
    return path


def update_outcomes(root):
    base=root/'observer_data'; base.mkdir(parents=True,exist_ok=True)
    lock=(base/'outcomes.lock').open('a')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: lock.close(); return 2
    runid=now().strftime('%Y%m%d_%H%M%S_%f')
    market=Market(base/'outcomes/raw'/runid)
    out=base/'outcomes/latest.json'
    old=json.loads(out.read_text(encoding='utf-8')) if out.exists() else {'records':{}}
    records=old.get('records',{}); errors=[]; today=now().date().isoformat(); deadline=time.monotonic()+540
    try:
        calendar=market_calendar(market,deadline)
        completed=[d for d in sorted(calendar) if d<today or (d==today and now()>=at(today,'15:10'))]
        cache={}; selected=[]
        for path in sorted((base/'live').glob('*/signals.json')):
            report=json.loads(path.read_text(encoding='utf-8'))
            selected.extend(r for r in report.get('rows',[]) if any(r['groups'].values()))
        for row in selected:
            key=row['date']+'|'+row['code']; existing=records.get(key,{})
            if all(existing.get('horizons',{}).get(str(h),{}).get('status')=='available' for h in (1,3,5)):
                continue
            if time.monotonic()>=deadline:
                errors.append({'key':key,'error':'deadline'}); continue
            try:
                if row['code'] not in cache:
                    raw=rows_of(market.call('bars',deadline,code=row['code'],period='day',count=160),('bars',))
                    cache[row['code']]=daily_map(raw)
                values={str(h):outcome_for(row,cache[row['code']],completed,h) for h in (1,3,5)}
                # Freeze completed horizon values; later runs only fill missing horizons.
                for h,v in existing.get('horizons',{}).items():
                    if v.get('status')=='available': values[h]=v
                records[key]={'horizons':values,'updated_at':stamp()}
            except Exception as exc: errors.append({'key':key,'error':type(exc).__name__})
        result={'updated_at':stamp(),'records':records,'errors':errors,'request_timings':market.events}
        write_json(base/'outcomes/history'/f'{runid}.json',result,immutable=True)
        write_json(out,result)
        log('收益与周度汇总：'+str(summary_tables(root,records)))
        return 2 if errors else 0
    except Exception as exc:
        write_json(base/'outcomes/history'/f'error_{runid}.json',{'time':stamp(),'error':type(exc).__name__},immutable=True)
        log('收益更新失败，保留上次结果：'+type(exc).__name__); return 2
    finally: market.close(); lock.close()


def doctor(root):
    runid=now().strftime('%Y%m%d_%H%M%S_%f'); directory=root/'observer_data/doctor'/runid
    market=Market(directory/'raw'); deadline=time.monotonic()+45
    result={'started_at':stamp(),'notification':'not_sent','rules_hash':CONFIG_HASH}
    try:
        result['version']=market.call('version',deadline)
        rank=rows_of(market.call('rank',deadline,category='沪深A股',sort_by='涨幅',start=0,count=80,ascending=False),('records',))
        valid=[]
        for row in rank:
            if code_of(row):
                quote_values(row); number(row['total_hand']); valid.append(row)
        if not valid: raise ValueError('no_valid_rank_records')
        changes=[quote_values(r)[2] for r in valid]
        if any(a+1e-7<b for a,b in zip(changes,changes[1:])): raise ValueError('rank_not_descending')
        code=code_of(valid[0]); result.update(rank_rows=len(rank),valid_rows=len(valid),sample_code=code)
        profile=rows_of(market.call('profile',deadline,codes=[code]),('rows',))
        match=[p for p in profile if code_of(p)==code and number(p.get('circulating_shares',0))>0]
        if not match: raise ValueError('missing_profile_shares')
        calendar=market_calendar(market,deadline)
        result['latest_index_session']=max(calendar)
        result['status']='passed'
        log('接口检查通过：榜单排序、行情字段、股本、指数日历均已返回')
    except Exception as exc:
        result.update(status='failed',error=type(exc).__name__,reason=str(exc)[:120] if isinstance(exc,(ValueError,FeedError)) else 'see_installed_eltdx_version_and_network')
        log('接口检查失败：'+result['error'])
    finally:
        market.close(); result.update(finished_at=stamp(),timings=market.events)
        write_json(directory/'doctor.json',result,immutable=True); log('检查报告：'+str(directory/'doctor.json'))
    return 0 if result['status']=='passed' else 2


def load_env(path):
    if not path.is_file(): return
    # Read only the one supported notification key; never source arbitrary shell code.
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if line.startswith('export '): line=line[7:].strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k,v=line.split('=',1)
        if k.strip()=='SERVERCHAN_SENDKEY':
            value=v.strip()
            if len(value)>=2 and value[0]==value[-1] and value[0] in ('"',"'"): value=value[1:-1]
            os.environ.setdefault(k.strip(),value)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command',choices=('doctor','rehearse','run','update'))
    ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    ap.add_argument('--notify',action='store_true',help='send research/status notification to SERVERCHAN_SENDKEY; live mode only')
    ap.add_argument('--env-file',type=Path,help='optional file with SERVERCHAN_SENDKEY; no shell evaluation')
    args=ap.parse_args(); root=args.root.resolve()
    if args.env_file: load_env(args.env_file.resolve())
    if args.command=='doctor': return doctor(root)
    if args.command=='update': return update_outcomes(root)
    return run_observation(root,'live' if args.command=='run' else 'rehearsal',args.notify)


if __name__=='__main__': sys.exit(main())
