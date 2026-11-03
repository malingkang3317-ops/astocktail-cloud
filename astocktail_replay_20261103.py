#!/usr/bin/env python3
"""Read-only, retrospective candidate replay of 2026-11-03 at 14:50 China time.

Important limitations: Post-close gain-ranked candidate pool has selection bias;
minute last price is a proxy for last trade; daily minute data have no signed
exchange timestamps. This script NEVER claims an exhaustive live 14:50 scan.
"""
import argparse
import json
import math
import statistics
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from eltdx import TdxClient, to_jsonable

DAY = date(2026, 11, 3)
DATE = DAY.isoformat()
TZ = ZoneInfo('Asia/Shanghai')
OUT = Path(__file__).resolve().parent / 'replay_reports'
CUTOFF = '14:49'  # 14:50 decision: conservative last confirmed minute (14:49)
INDEX = {'sh': 'sh000001', 'sz': 'sz399001'}


def index_for(code):
    if code.startswith('sz30'): return 'sz399006'
    if code.startswith('sh68'): return 'sh000688'
    return INDEX[code[:2]]


def is_ashare(code):
    return (code.startswith('sh60') or code.startswith('sh68') or
            code.startswith('sz00') or code.startswith('sz30')) and len(code) == 8


def ptime(point):
    return point.get('time_label', '')[:5]


def as_points(data):
    return to_jsonable(data).get('points', [])


def hist_bars(client, code):
    bars = to_jsonable(client.bars.get(code, period='day', count=15)).get('bars', [])
    recent = []
    for b in bars:
        if not b.get('time'): continue
        try: d = datetime.fromisoformat(b['time']).date()
        except Exception: continue
        if d >= DAY: continue
        v = b.get('volume_lots')
        if v is not None and float(v) > 0: recent.append((d, float(v)))
    recent = sorted(dict(recent).items())[-5:]
    if len(recent) != 5: raise ValueError('历史有效日成交量少于5个交易日')
    return recent


def minute_data(client, code):
    # Explicit historical date avoids accidentally accepting another trading day.
    data = client.minutes.history(code, DATE)
    result = to_jsonable(data)
    points = result.get('points') or []
    if not points:
        raise ValueError('指定日期分钟行情为空')
    valid = [p for p in points if '09:31' <= ptime(p) <= CUTOFF or '13:01' <= ptime(p) <= CUTOFF]
    valid = [p for p in valid if p.get('price') is not None and p.get('volume') is not None and p.get('avg_price') is not None]
    by_time = {ptime(p): p for p in valid}
    valid = [by_time[k] for k in sorted(by_time)]
    if len(valid) < 220 or ptime(valid[-1]) < '14:47':
        raise ValueError(f'可验证分钟数据不足或不新: count={len(valid)}, last={ptime(valid[-1]) if valid else "none"}')
    return valid


def basics_from_minutes(points, prevclose, shares, history):
    price = float(points[-1]['price'])
    change = (price / prevclose - 1) * 100
    volume = sum(float(p['volume']) for p in points)  # eltdx MinutePoint volume = lots
    n = len(points)
    vol_ratio = (volume/n) / (statistics.mean(v for _, v in history) / 240)
    turnover = volume * 100 / shares * 100
    cap = price * shares
    return {'price_1449': round(price, 4), 'change_pct': round(change, 3),
            'volume_ratio': round(vol_ratio, 3), 'turnover_pct': round(turnover, 3),
            'floating_cap_yuan': round(cap, 2), 'volume_lots_1449': round(volume, 2),
            'minute_count': n, 'last_minute': ptime(points[-1]),
            'volume_ratio_definition': '当日截至14:49平均每分钟成交量/前5完整交易日平均每分钟成交量'}


def technical(points, idx, config):
    idx_map = {ptime(p): float(p.get('close', p.get('price', 0))) for p in idx}
    matched = [(p, idx_map[ptime(p)]) for p in points if ptime(p) in idx_map and idx_map[ptime(p)] > 0]
    if len(matched) < 210: raise ValueError(f'指数分钟对齐不足: {len(matched)}')
    spr = (float(matched[-1][0]['price']) / float(matched[0][0]['price']) - 1) * 100
    ir = (matched[-1][1]/matched[0][1]-1)*100
    relative = spr - ir
    above_frac = sum(float(p['price']) >= float(p['avg_price']) for p in points)/len(points)
    # Only complete 5-min blocks, compare steady demand with early afternoon.
    vmap = {ptime(p): float(p['volume']) for p in points}
    def volume_block(h, m):
        times = [f'{h:02d}:{k:02d}' for k in range(m,m+5)]
        if not all(k in vmap for k in times): raise ValueError(f'缺少完整5分钟成交区间 {times[0]}')
        return sum(vmap[k] for k in times)
    base = [volume_block(13,m) for m in (50,55)] + [volume_block(14,m) for m in (0,5)]
    tail = [volume_block(14,m) for m in (20,25,30,35)]
    segment = [p for p in points if '13:30' <= ptime(p) <= CUTOFF]
    max_share = max(float(p['volume']) for p in segment)/max(1,sum(float(p['volume']) for p in segment))
    steps = sum(tail[i] > tail[i-1] for i in range(1,len(tail)))
    step_ok = statistics.mean(tail)>1.12*statistics.mean(base) and steps >= 2 and max_share <= .13
    prior = [p for p in points if ptime(p) < '14:43']
    peakwin = [p for p in points if '14:43' <= ptime(p) <= '14:47']
    if not prior or not peakwin: raise ValueError('尾盘突破窗口分钟记录缺失')
    peak = max(float(p['price']) for p in peakwin)  # price proxy, not intra-minute HIGH
    peak_pos = max(i for i,p in enumerate(points) if p in peakwin and float(p['price']) == peak)
    after = points[peak_pos+1:]
    new_high = peak > max(float(p['price']) for p in prior)
    if after:
        low = min(float(p['price']) for p in after)
        pullback = (peak-low)/peak
        vwap_holds = all(float(p['price']) >= float(p['avg_price'])*.999 for p in after)
        pullback_ok = .0003 <= pullback <= .012 and vwap_holds
    else:
        low, pullback, vwap_holds, pullback_ok = None,None,False,False
    flags = {'成交量台阶放大':step_ok, '强于对应指数':relative>0,
             '价格基本位于均价线上':above_frac>=.70,
             '14:45附近创分钟收盘新高':new_high, '创新高后分钟价回踩守均价':pullback_ok}
    return {'flags':flags,'relative_pct_points':round(relative,3),
            'price_above_avg_ratio':round(above_frac,3),'volume_tail_over_base':round(statistics.mean(tail)/statistics.mean(base),3),
            'volume_rising_blocks':steps,'largest_minute_share':round(max_share,4),
            'peak_price_proxy':peak, 'pullback_low_price_proxy':low,
            'pullback_fraction':pullback, 'vwap_holds_on_minute_prices':vwap_holds,
            'caveat':'分钟收盘价替代分钟最高/最低价，不能验证分钟内真实回踩低点'}


def run(args):
    if datetime.now(TZ).date() < DAY:
        raise RuntimeError('今天尚未结束，历史回放不能提前运行')
    OUT.mkdir(parents=True,exist_ok=True)
    report = {'date':DATE, 'decision_cutoff':'14:50 Asia/Shanghai',
              'last_confirmed_minute_used':CUTOFF,
              'mode':'postclose_replay_partial_coverage',
              'limitations':['候选池取自收盘后涨幅排行榜，不是14:30原始候选池，可能漏掉14:50符合但15:00不在预筛区间的股票',
                             '分钟数据无交易所逐条签名时间戳；仅按历史交易日和分钟标签截断',
                             '技术形态使用分钟收盘价格，无法证明分钟内最高/最低或即时成交顺序',
                             '流通股本当前基础资料需确认在回放日14:50之前已生效',
                             '历史回放不等于实时策略、不能保证次日上涨'],
              'scan':{},'candidates':[], 'strictly_verified':[], 'errors':[], 'summary':{}}
    begun=time.monotonic()
    candidates={}
    with TdxClient(timeout=10) as client:
        previous_last=1e6
        reached_floor=False
        for offset in range(0,args.max_pages*80,80):
            pg=to_jsonable(client.quotes.list_by_category('沪深A股',sort_by='涨幅',start=offset,count=80,ascending=False))
            rows=pg.get('records') or []
            if not rows: report['errors'].append(f'榜单分页{offset}为空');break
            pct_list=[]
            for r in rows:
                price=float(r.get('last_price') or 0); prev=float(r.get('pre_close_price') or 0)
                if price<=0 or prev<=0: continue
                gain=100*(price/prev-1)
                pct_list.append(gain)
                code=str(r.get('exchange','')).lower()+str(r.get('code','')).zfill(6)
                if 2<=gain<=6 and is_ashare(code):candidates[code]=prev
            if not pct_list: report['errors'].append(f'榜单{offset}无有效价格');break
            if any(pct_list[i]<pct_list[i+1]-.03 for i in range(len(pct_list)-1)) or pct_list[0]>previous_last+.03:
                report['errors'].append(f'榜单跨页或页内排序异常({offset})');break
            previous_last=pct_list[-1]
            print(f'榜单{offset}: {len(rows)}条, 最低涨幅{pct_list[-1]:.2f}%, 累计候选{len(candidates)}',flush=True)
            if pct_list[-1]<2:
                reached_floor=True;break
        report['scan']={'gain_rank_candidates_postclose':len(candidates),'reached_2pct_floor':reached_floor,
                        'range':'15:00左右涨幅2%-6%（非14:30实时初筛）'}
        if not reached_floor:
            report['errors'].append('未覆盖完整的收盘后2%-6%区间')
        index_cache={}
        codes=list(candidates)
        for k in range(0,len(codes),args.batch):
            batch=codes[k:k+args.batch]
            print(f'资料批次 {k+1}-{k+len(batch)}/{len(codes)}',flush=True)
            try:
                profile=to_jsonable(client.helpers.stock_profile_table(batch)).get('rows',[])
            except Exception as e:
                report['errors'].append(f'资料批次{k}: {str(e)[:150]}');continue
            for r in profile:
                code=r.get('full_code')
                if code not in candidates: continue
                try:
                    shares=float(r.get('circulating_shares') or 0)
                    if shares<=0:raise ValueError('流通股本缺失')
                    prev=candidates[code]
                    # Explicitly dated historical minutes: reconstruct 14:49 price & cumulative volume.
                    pts=minute_data(client,code)
                    baselines=hist_bars(client,code)
                    base=basics_from_minutes(pts,prev,shares,baselines)
                    primary=(3<=base['change_pct']<=5 and base['volume_ratio']>1 and
                             5<=base['turnover_pct']<=10 and 5e9<=base['floating_cap_yuan']<=20e9)
                    if not primary: continue
                    idxcode=index_for(code)
                    if idxcode not in index_cache:
                        idxrows=to_jsonable(client.bars.get(idxcode,period='1m',count=300,kind='index')).get('bars',[])
                        index_cache[idxcode]=[{'time_label':datetime.fromisoformat(b['time']).strftime('%H:%M'),
                         'close':b['close']} for b in idxrows if b.get('time') and
                         datetime.fromisoformat(b['time']).date()==DAY and datetime.fromisoformat(b['time']).strftime('%H:%M')<=CUTOFF]
                    tech=technical(pts,index_cache[idxcode],None)
                    technical_pass=all(tech['flags'].values())
                    result={'code':code,'name':r.get('name'),'data_date':DATE,'basis_last_minute':base['last_minute'],
                            'circulating_shares_update':r.get('updated_date'),
                            'basic':base,'technical':tech,'basic_pass':True,'technical_proxy_pass':technical_pass,
                            'strictly_verified':False,'reason':'回放候选池偏差及分钟内高低点/逐股原始快照不可追溯，非严格实时8项验证'}
                    report['candidates'].append(result)
                    print(f"基础通过 {code} {r.get('name')} 技术分钟代理={'通过' if technical_pass else '未过'}",flush=True)
                except Exception as e:
                    report['errors'].append(f'{code}: {type(e).__name__}: {str(e)[:160]}')
            if args.max_minutes and (time.monotonic()-begun)/60>args.max_minutes:
                report['errors'].append('用户设置的最长运行时限已到，回放不完整');break
    report['candidates'].sort(key=lambda x:(x['technical_proxy_pass'],x['basic']['volume_ratio']),reverse=True)
    report['summary']={'basic_screen_count':len(report['candidates']),
                       'minute_proxy_pass_count':sum(x['technical_proxy_pass'] for x in report['candidates']),
                       'strict_14_50_count':0,'reason':'只有收盘后可追溯的代理数据；不能自称14:50全市场严格实时结果',
                       'elapsed_seconds':round(time.monotonic()-begun,2), 'errors':len(report['errors'])}
    path=OUT / 'replay_2026-11-03.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('\n=== 回放汇总 ===')
    print(json.dumps(report['summary'],ensure_ascii=False,indent=2))
    print('结果文件:',path)
    for item in report['candidates'][:10]:
        print(item['code'],item['name'],'涨幅',item['basic']['change_pct'],'量比',item['basic']['volume_ratio'],
              '换手率',item['basic']['turnover_pct'],'市值(亿)',round(item['basic']['floating_cap_yuan']/1e8,2),
              '分钟代理',item['technical_proxy_pass'])

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--batch',type=int,default=20)
    ap.add_argument('--max-pages',type=int,default=30)
    ap.add_argument('--max-minutes',type=int,default=25,help='0=no limit')
    run(ap.parse_args())
