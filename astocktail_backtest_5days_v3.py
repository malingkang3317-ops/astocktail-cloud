#!/usr/bin/env python3
"""AStockTail revised-technical-rule five-day historical diagnostic replay (NOT live selections).

The historical daily HIGH/LOW must intersect the strict 3%-5% band for a
symbol to be a candidate. This is a *necessary* filter for the 14:49 price,
not a sufficient criterion. Thus daily end-of-day gain does not leak into
candidate filtering. Current symbol universe is incomplete for delistings.

Historical circulating shares from today's profile are NOT proven as-of each
past day: output is provisional, never an as-of strict result. Dataset may
also include corporate-action differences, and minute OHLC cannot establish
intraminute event order. No server-side alerts or GitHub changes.
"""
import argparse
import csv
import json
import math
import statistics
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from eltdx import TdxClient, to_jsonable

TZ = ZoneInfo('Asia/Shanghai')
DAYS = ['2026-10-28', '2026-10-29', '2026-10-30', '2026-11-02', '2026-11-03']
OUT = Path(__file__).resolve().parent / 'replay_reports' / 'five_days_v3'
CACHE = Path(__file__).resolve().parent / 'replay_reports' / 'five_days' / 'cache'  # reuse verified V2 historical cache
CUTOFF = '14:49'
INDEX = {'sh': 'sh000001', 'sz': 'sz399001'}


def code_ok(code):
    return len(code) == 8 and code.startswith(('sh60','sh68','sz00','sz30'))


def index_for(code):
    if code.startswith('sz30'): return 'sz399006'
    if code.startswith('sh68'): return 'sh000688'
    return INDEX[code[:2]]


def day_from_bar(row):
    return str(row['time'])[:10]


def write_json(path, obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(path)


def cached_fetch(cache_path, get_data, *, refresh=False):
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text(encoding='utf-8'))
    result=get_data()
    write_json(cache_path,result)
    return result


def get_days(client, code, *, refresh=False):
    path=CACHE / 'daily' / f'{code}.json'
    return cached_fetch(path, lambda:to_jsonable(client.bars.get(code,period='day',count=25)).get('bars',[]),refresh=refresh)


def prior_five(dailies, target):
    hist=[]
    for b in dailies:
        d=day_from_bar(b)
        v=float(b.get('volume_lots') or 0)
        if d<target and v>0: hist.append((d,v))
    hist=sorted(dict(hist).items())[-5:]
    return hist if len(hist)==5 else None


def daily_possible(bars,target):
    by_date={day_from_bar(b):b for b in bars if b.get('time')}
    row=by_date.get(target)
    prev=[b for d,b in sorted(by_date.items()) if d < target]
    if not row or not prev:return None,'日线或昨收缺失'
    # Today's daily bar open/hi/lo may be affected by corporate actions: flag, not mask.
    prev_close=float(prev[-1].get('close') or 0)
    low=float(row.get('low') or 0)
    high=float(row.get('high') or 0)
    if min(prev_close,low,high)<=0 or high<low:return None,'日线OHLC异常'
    return (high/prev_close>=1.03 and low/prev_close<=1.05),None


def get_minutes(client,code, *,refresh=False):
    # 2 x 800 one-minute bars cover 5 recent trading days (~1200 bars).
    path=CACHE / 'minutes' / f'{code}.json'
    def fetch():
        pages=[]
        for start in (0,800):
            pg=to_jsonable(client.bars.get(code,period='1m',start=start,count=800)).get('bars',[])
            pages.extend(pg)
        return pages
    return cached_fetch(path,fetch,refresh=refresh)


def points_at(raw,target):
    points=[]
    for b in raw:
        if not b.get('time'):continue
        ts=datetime.fromisoformat(b['time']).astimezone(TZ)
        tm=ts.strftime('%H:%M')
        if ts.date().isoformat()!=target or tm>CUTOFF:continue
        if not ('09:31'<=tm<='11:30' or '13:01'<=tm<=CUTOFF):continue
        p={k:float(b.get(k) or 0) for k in ('close','high','low','amount','volume_lots')}
        if p['close']<=0 or p['high']<p['low'] or p['low']<=0 or p['volume_lots']<0 or p['amount']<0:
            raise ValueError(f'{tm}:无效分钟OHLCV')
        points.append({'time_label':tm,'price':p['close'],'high':p['high'],'low':p['low'],
                       'amount':p['amount'],'volume':p['volume_lots']})
    points.sort(key=lambda x:x['time_label'])
    expected=[f'{h:02d}:{m:02d}' for h,lo,hi in ((9,31,59),(10,0,59),(11,0,30),(13,1,59),(14,0,49)) for m in range(lo,hi+1)]
    actual=[p['time_label'] for p in points]
    if actual!=expected:
        raise ValueError(f'分钟不完整 {len(actual)}/{len(expected)}, 缺失:{list(sorted(set(expected)-set(actual)))[:4]}')
    total_amount=0.; total_shares=0.
    for p in points:
        total_amount+=p['amount'];total_shares+=p['volume']*100
        if total_shares<=0:raise ValueError('成交股数为零')
        p['avg_price']=total_amount/total_shares
    return points


def index_points(client, code, target, *,refresh=False):
    path=CACHE / 'indices' / f'{code}.json'
    def fetch():
        arr=[]
        for start in (0,800):
            arr.extend(to_jsonable(client.bars.get(code,period='1m',kind='index',start=start,count=800)).get('bars',[]))
        return arr
    raw=cached_fetch(path,fetch,refresh=refresh)
    ret={}
    for b in raw:
        ts=datetime.fromisoformat(b['time']).astimezone(TZ)
        hm=ts.strftime('%H:%M')
        if ts.date().isoformat()==target and hm<=CUTOFF:
            ret[hm]=float(b['close'])
    return ret


def technical(points, idx):
    """Historical minute-bar proxies for the user's revised 6th and 8th rules.

    Volume: split 10:00..14:49 into five chronological groups of 40 trading
    minutes; the last median must exceed the mean of medians for groups 3,4.
    Breakout: a 14:40..14:49 1m high must exceed the prior 13:01..current
    intraday afternoon high, followed by a *later completed minute* with a
    0.05% pullback and a low not below its own VWAP. OHLC is not tick data.
    """
    matched=[(p,idx[p['time_label']]) for p in points if p['time_label'] in idx and idx[p['time_label']]>0]
    if len(matched)<220:raise ValueError(f'指数数据对齐不足:{len(matched)}')
    rel=(matched[-1][0]['price']/matched[0][0]['price']-1)*100-(matched[-1][1]/matched[0][1]-1)*100
    above=sum(p['price']>=p['avg_price'] for p in points)/len(points)
    seq=[p for p in points if ('10:00'<=p['time_label']<='11:30') or ('13:01'<=p['time_label']<=CUTOFF)]
    if len(seq)!=200:raise ValueError(f'10点后分钟数:{len(seq)}')
    medians=[statistics.median(x['volume'] for x in seq[i:i+40]) for i in range(0,200,40)]
    baseline=(medians[2]+medians[3])/2
    if baseline<=0:raise ValueError('第三、四段基准中位数均值不大于0')
    volume_ok=medians[4]>baseline
    # Diagnostic only; no longer a mandatory three-increase test.
    steps=sum(medians[i]>medians[i-1] for i in range(1,5))
    window=[]
    afternoon_high=float('-inf')
    for i,p in enumerate(points):
        hm=p['time_label']
        if not ('13:01'<=hm<=CUTOFF):
            continue
        if '14:40'<=hm<=CUTOFF and p['high']>afternoon_high:
            window.append(i)
        afternoon_high=max(afternoon_high,p['high'])
    pullback=None
    for i in window:
        high=points[i]['high']
        # This is a conservative bar-level proxy: pullback must occur in a
        # subsequent completed minute, not within the breakout minute.
        for j in range(i+1,len(points)):
            low=points[j]['low']
            drop=(high-low)/high
            if drop>=0.0005 and low>=points[j]['avg_price']:
                # Retain the previous safety rule: no intervening/later
                # completed-minute low may breach the minute's VWAP.
                if all(p['low']>=p['avg_price'] for p in points[i+1:]):
                    pullback={'high_minute':points[i]['time_label'],
                              'pullback_minute':points[j]['time_label'],
                              'drop_pct':round(drop*100,4)}
                    break
        if pullback:break
    flags={'volume_tail_above_prior_baseline':volume_ok,
           'stronger_than_index':rel>0,
           'vwap_above_70pct':above>=0.7,
           'breakout_above_1300_high':bool(window),
           'pullback_005pct_hold_vwap':bool(pullback)}
    return {'flags':flags,'pass':all(flags.values()),'volume_medians':medians,
            'volume_baseline_medians_3_4_mean':round(baseline,4),
            'volume_tail_over_baseline':round(medians[4]/baseline,4),
            'volume_rising_transitions_diagnostic_only':steps,
            'stock_outperformance_pct_points':round(rel,4),'above_vwap_fraction':round(above,4),
            'tail_breakout_minutes':[points[i]['time_label'] for i in window],
            'qualified_pullback':pullback}

def evaluate(bars,minute_rows,idx,shares,target):
    pts=points_at(minute_rows,target)
    daily_map={day_from_bar(x):x for x in bars}
    prior=[(d,b) for d,b in sorted(daily_map.items()) if d<target]
    if not prior:raise ValueError('缺少昨收')
    prev=float(prior[-1][1]['close'])
    hist=prior_five(bars,target)
    if not hist:raise ValueError('历史5日成交量不足')
    vol=sum(p['volume'] for p in pts)
    last=pts[-1]['price']; n=len(pts)
    change=(last/prev-1)*100
    ratio=(vol/n)/(statistics.mean(v for _,v in hist)/240)
    output={'price':round(last,3),'change_pct':round(change,3),
            'volume_ratio':round(ratio,3),'volume_ratio_definition':'当日截至14:49每分钟均量/过去5个完整交易日平均每分钟成交量',
            'volume_lots':round(vol,2),'minute_count':n,'minute_end':pts[-1]['time_label']}
    if shares and shares>0:
        output.update({'turnover_pct_proxy':round(vol*100/shares*100,3),
                       'floating_market_cap_yi_proxy':round(last*shares/1e8,3)})
    basic_without_shares=(3<=change<=5 and ratio>1)
    profile_proxy=(shares is not None and shares>0 and 5<=vol*100/shares*100<=10 and 50<=last*shares/1e8<=200)
    tech=technical(pts,idx) if basic_without_shares and profile_proxy else None
    return output,basic_without_shares,profile_proxy,tech


def run(args):
    OUT.mkdir(parents=True,exist_ok=True)
    summary={'mode':'historical_replay_provisional','days':DAYS,'cutoff':'14:49 completed minute',
             'limitations':['当前代码表可能遗漏历史退市证券，存在生存者偏差',
                            '使用当前流通股本不能证明历史日换手率和流通市值，需要历史股本变更资料',
                            '1分钟历史OHLC只能近似判断事件，无法验证分钟内事件顺序',
                            '历史数据可能存在后复权/权息调整差异及行情历史修正',
                            '未复现14:30时点股票池，这里采用历史整日最高/最低价必要条件缩小范围',
                            'V3规则：10点后五段分钟量中位数，第5段严格大于第3和第4段中位数的平均值；不要求3次抬升',
                            'V3规则：14:40-14:49分钟高点突破当天下午13:01以来此前高点，后续完整分钟回落至少0.05%，且期间低点不破当时VWAP',
                            '不是实时14:50推荐，严格实时确认计数固定为0'],
             'per_day':{},'errors_sample':[]}
    for d in DAYS:
        summary['per_day'][d]={'daily_seen':0,'daily_missing':0,'daily_range_candidates':0,
                               'minute_attempted':0,'minute_valid':0,'basic_price_volume_pass':0,
                               'profile_proxy_pass':0,'technical_proxy_pass':0,'failed':0,'complete':False,
                               'top_candidates':[]}
    start=time.monotonic()
    with TdxClient(timeout=10) as client:
        symbols=client.codes.all_a_shares()
        if isinstance(symbols, dict):
            symbols = symbols.get('rows') or symbols.get('items') or []
        codes = set()
        for s in symbols:
            if isinstance(s, str):
                full_code = s.lower()
            elif isinstance(s, dict):
                full_code = str(s.get('full_code') or
                                (str(s.get('exchange', '')) + str(s.get('code', '')))).lower()
            else:
                full_code = str(getattr(s, 'full_code', '') or
                                (str(getattr(s, 'exchange', '')) + str(getattr(s, 'code', '')))).lower()
            if code_ok(full_code):
                codes.add(full_code)
        codes = sorted(codes)
        if not codes:raise RuntimeError('未能提取A股代码表，检查eltdx版本')
        summary['universe_size']=len(codes)
        if args.max_stocks:
            codes=codes[:args.max_stocks]
            summary['subset_only']=True
        else:summary['subset_only']=False
        print('股票代码数量:',len(codes),'全市场:',not summary['subset_only'],flush=True)
        selected={d:[] for d in DAYS}
        daily={}
        for i,code in enumerate(codes,1):
            try:
                bars=get_days(client,code,refresh=args.refresh)
                daily[code]=bars
                for d in DAYS:
                    state=summary['per_day'][d]
                    ok,err=daily_possible(bars,d)
                    if err:state['daily_missing']+=1
                    else:
                        state['daily_seen']+=1
                        if ok:
                            state['daily_range_candidates']+=1
                            selected[d].append(code)
            except Exception as e:
                summary['errors_sample'].append(f'日线 {code}: {type(e).__name__} {str(e)[:110]}')
            if i%100==0:
                print(f'历史日线 {i}/{len(codes)}；已缓存；累计 {time.monotonic()-start:.1f}s',flush=True)
                write_json(OUT/'progress.json',summary)
        # Ensure profile values are clearly as-of CURRENT rather than each past day.
        union=sorted(set().union(*[set(x) for x in selected.values()]))
        print('历史日内涨幅区间可能触及3-5%的股票:',len(union),flush=True)
        profiles={}
        for i in range(0,len(union),args.batch):
            batch=union[i:i+args.batch]
            try:
                for p in to_jsonable(client.helpers.stock_profile_table(batch)).get('rows',[]):
                    profiles[p.get('full_code')]=p
            except Exception as e:
                summary['errors_sample'].append(f'股本批次 {i}: {type(e).__name__} {str(e)[:110]}')
        index_maps={}
        for d in DAYS:
            state=summary['per_day'][d]
            for code in selected[d]:
                state['minute_attempted']+=1
                try:
                    raw=get_minutes(client,code,refresh=args.refresh)
                    p=profiles.get(code,{})
                    shares=float(p.get('circulating_shares') or 0) or None
                    idx=index_for(code)
                    if idx not in index_maps:
                        index_maps[idx]={k:index_points(client,idx,k,refresh=args.refresh) for k in DAYS}
                    basic,basok,profileok,tech=evaluate(daily[code],raw,index_maps[idx][d],shares,d)
                    state['minute_valid']+=1
                    if basok:state['basic_price_volume_pass']+=1
                    if basok and profileok:state['profile_proxy_pass']+=1
                    if tech and tech['pass']:state['technical_proxy_pass']+=1
                    if basok and profileok:
                        next_days=sorted((day_from_bar(b),float(b.get('close') or 0)) for b in daily[code]
                                         if day_from_bar(b)>d and float(b.get('close') or 0)>0)
                        next_day_return=(round((next_days[0][1]/basic['price']-1)*100,3) if next_days else None)
                        state['top_candidates'].append({'code':code,'name':p.get('name'),'basic':basic,
                          'next_trading_day_close_return_pct_vs_1449':next_day_return,
                          'profile_updated_date':p.get('updated_date'),
                          'historical_shares_verified':False,'technical':tech,
                          'strictly_verified':False})
                except Exception as e:
                    state['failed']+=1
                    if len(summary['errors_sample'])<40:
                        summary['errors_sample'].append(f'{d} {code}: {type(e).__name__} {str(e)[:120]}')
                if state['minute_attempted']%75==0:
                    print(f'{d} 分钟验证 {state["minute_attempted"]}/{len(selected[d])}，技术代理{state["technical_proxy_pass"]}',flush=True)
                    write_json(OUT/'progress.json',summary)
            state['top_candidates'].sort(key=lambda x: (bool(x['technical'] and x['technical']['pass']),x['basic']['volume_ratio']),reverse=True)
            # Preserve all proxy candidates for audit, not only the top 30.
            # These entries are NOT strictly validated historical selections.
            state['complete']=state['failed']==0 and state['daily_missing']==0 and not summary['subset_only']
            print(d,'日K触及',state['daily_range_candidates'],'基础代理',state['profile_proxy_pass'],'技术代理',state['technical_proxy_pass'],'失败',state['failed'],flush=True)
            write_json(OUT/'progress.json',summary)
    summary['elapsed_seconds']=round(time.monotonic()-start,2)
    summary['next_day_outcome_note']='仅为回放技术代理命中股票的次日收盘/当日14:49价格收益率，未扣交易成本，非真实可执行收益；11月3日次日尚无数据。'
    summary['complete_full_coverage']=all(s['complete'] for s in summary['per_day'].values())
    summary['strict_verified_count']=0
    write_json(OUT/'five_day_report.json',summary)
    with (OUT/'five_day_summary.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.writer(f)
        writer.writerow(['date','daily_seen','daily_missing','daily_range_candidates','minute_valid','basic_price_volume_pass',
                         'profile_proxy_pass','technical_proxy_pass','failed','complete'])
        for d,s in summary['per_day'].items():
            writer.writerow([d]+[s[k] for k in ('daily_seen','daily_missing','daily_range_candidates','minute_valid','basic_price_volume_pass',
                                              'profile_proxy_pass','technical_proxy_pass','failed','complete')])
    print('\n=== 五日回放汇总（代理，不是实时推荐） ===')
    for d,s in summary['per_day'].items():
        print(d, '候选',s['daily_range_candidates'],'基础代理',s['profile_proxy_pass'],
              '技术代理',s['technical_proxy_pass'],'异常',s['failed'],'完整覆盖',s['complete'])
    print('合计耗时(s)',summary['elapsed_seconds'],'严格实时确认',0)
    print('输出:',OUT/'five_day_report.json')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--max-stocks',type=int,default=0,help='only smoke-test first N codes; 0 means all')
    parser.add_argument('--batch',type=int,default=20)
    parser.add_argument('--refresh',action='store_true',help='refresh cached remote data')
    args=parser.parse_args()
    run(args)
