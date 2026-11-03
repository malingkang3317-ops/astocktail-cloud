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
import sys
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
    """Single-source dated 1m OHLCV+amount. Rebuild VWAP from same bars.

    Close at 14:49 is the final *completed* minute used; no after-cutoff bars
    are used in price, volume, VWAP, or price-pattern decisions.
    """
    raw = to_jsonable(client.bars.get(code, period='1m', count=300)).get('bars') or []
    points = []
    for b in raw:
        if not b.get('time'):
            continue
        dt = datetime.fromisoformat(b['time']).astimezone(TZ)
        tm = dt.strftime('%H:%M')
        if dt.date() != DAY or tm > CUTOFF:
            continue
        if not ('09:31' <= tm <= '11:30' or '13:01' <= tm <= CUTOFF):
            continue
        vol = float(b.get('volume_lots') or 0)
        amount = float(b.get('amount') or 0)
        price = float(b.get('close') or 0)
        hi, lo = float(b.get('high') or 0), float(b.get('low') or 0)
        if vol < 0 or amount < 0 or price <= 0 or hi < lo or lo <= 0:
            raise ValueError(f'分钟价格/成交量不合理: {code} {tm}')
        points.append({'time_label':tm, 'price':price,'high':hi,'low':lo,
                       'volume':vol,'amount':amount})
    points.sort(key=lambda x:x['time_label'])
    expected = [f'{h:02d}:{m:02d}' for h,lo,hi in ((9,31,59),(10,0,59),(11,0,30),
                                                    (13,1,59),(14,0,49)) for m in range(lo,hi+1)]
    # This should be 229 valid minutes through 14:49.
    actual = [x['time_label'] for x in points]
    if len(actual) != len(set(actual)) or actual != expected:
        missing = sorted(set(expected)-set(actual))
        extra = sorted(set(actual)-set(expected))
        raise ValueError(f'分钟不完整/重复: {len(actual)}/{len(expected)}, missing={missing[:5]}, extra={extra[:5]}')
    cum_vol_shares = 0.0
    cum_amount = 0.0
    for p in points:
        cum_vol_shares += p['volume'] * 100
        cum_amount += p['amount']
        if cum_vol_shares <= 0:
            raise ValueError('累计成交股数为零，无法计算VWAP')
        p['avg_price'] = cum_amount / cum_vol_shares
    return points


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


def technical(points, idx, config=None):
    """Explain each minute-level proxy with measurements; no intraminute sequencing claims."""
    idx_map = {ptime(p):float(p['close']) for p in idx if p.get('close') is not None}
    matched = [(p,idx_map[ptime(p)]) for p in points if ptime(p) in idx_map and idx_map[ptime(p)]>0]
    if len(matched) < 220:
        raise ValueError(f'指数分钟对齐不足: {len(matched)}')
    stock_ret = (matched[-1][0]['price']/matched[0][0]['price']-1)*100
    index_ret = (matched[-1][1]/matched[0][1]-1)*100
    relative = stock_ret-index_ret
    above_frac = sum(p['price']>=p['avg_price'] for p in points)/len(points)
    # 10:00-14:49, 200 trading minutes (no lunch interval), 5 equal segments.
    # Each segment uses the MEDIAN minute volume, resistant to isolated spikes.
    volume_points = [p for p in points if '10:00' <= ptime(p) <= '11:30' or
                     '13:01' <= ptime(p) <= CUTOFF]
    if len(volume_points) != 200:
        raise ValueError(f'10:00后有效分钟不足200: {len(volume_points)}')
    segments = [volume_points[i:i+40] for i in range(0, 200, 40)]
    medians = [statistics.median(float(p['volume']) for p in seg) for seg in segments]
    if medians[0] <= 0:
        raise ValueError('第1段分钟量中位数<=0，无法可靠计算放大倍数')
    steps = sum(medians[i] > medians[i-1] for i in range(1, 5))
    volume_ratio = medians[-1] / medians[0]
    volume_ok = volume_ratio >= 1.5 and steps >= 3

    # 14:50 decision: use completed 1m OHLC bars only through 14:49.
    # A high during 14:40-14:49 must exceed ALL previous bar highs.
    # Pullback must occur in a LATER minute and reach at least 0.05%,
    # while subsequent minute lows continue to hold their contemporaneous VWAP.
    new_high_candidates = []
    running_high = float('-inf')
    for i, p in enumerate(points):
        tm = ptime(p)
        if '14:40' <= tm <= CUTOFF and p['high'] > running_high:
            new_high_candidates.append(i)
        running_high = max(running_high, p['high'])
    new_high = bool(new_high_candidates)
    chosen_peak = None
    chosen_low = None
    chosen_pullback = None
    vwap_holds = False
    pullback_ok = False
    for i in new_high_candidates:
        later = points[i+1:]
        if not later:
            continue
        low = min(p['low'] for p in later)
        decline = (points[i]['high'] - low) / points[i]['high']
        holds = all(p['low'] >= p['avg_price'] for p in later)
        if decline >= .0005 and holds:
            chosen_peak = i
            chosen_low = low
            chosen_pullback = decline
            vwap_holds = True
            pullback_ok = True
            break
    # For diagnostics, show the first tail-window high even if pullback fails.
    if chosen_peak is None and new_high_candidates:
        chosen_peak = new_high_candidates[0]
        later = points[chosen_peak+1:]
        if later:
            chosen_low = min(p['low'] for p in later)
            chosen_pullback = (points[chosen_peak]['high'] - chosen_low) / points[chosen_peak]['high']
            vwap_holds = all(p['low'] >= p['avg_price'] for p in later)
    flags={
        '10:00后5段分钟量中位数阶梯放大（代理）':volume_ok,
        '分时收益跑赢指数（代理）':relative>0,
        '分钟收盘价70%以上不低于VWAP（代理）':above_frac>=.70,
        '14:40-14:49分钟最高价创日内新高（代理）':new_high,
        '创新高后至少回落0.05%且VWAP守住（代理）':pullback_ok,
    }
    return {'flags':flags,'stock_intraday_return_pct':round(stock_ret,3),
            'index_intraday_return_pct':round(index_ret,3),
            'relative_pct_points':round(relative,3),
            'price_above_avg_ratio':round(above_frac,3),
            'volume_last_over_first_median':round(volume_ratio,3),
            'volume_segment_medians_lots':[round(v,3) for v in medians],
            'volume_segment_ranges':[(ptime(seg[0]),ptime(seg[-1])) for seg in segments],
            'volume_rising_transitions':steps,
            'peak_minute':ptime(points[chosen_peak]) if chosen_peak is not None else None,
            'peak_high':points[chosen_peak]['high'] if chosen_peak is not None else None,
            'pullback_low':chosen_low,'pullback_fraction':chosen_pullback,
            'vwap_holds_on_following_minute_lows':vwap_holds,
            'failed_conditions':[k for k,v in flags.items() if not v],
            'caveat':'5段各40个交易分钟；分钟OHLC只能证明不同分钟先后，不能还原分钟内部价格顺序；这是历史代理验证'}


def run(args):
    if datetime.now(TZ).date() != DAY:
        raise RuntimeError('收盘后榜单只适用于2026-11-03当天；跨日无法还原当天候选池，禁止混用其他日期榜单')
    if datetime.now(TZ).strftime('%H:%M') < '15:01':
        raise RuntimeError('请在目标交易日15:01以后运行，以避免使用未结束的收盘榜单')
    OUT.mkdir(parents=True,exist_ok=True)
    report = {'date':DATE, 'decision_cutoff':'14:50 Asia/Shanghai',
              'last_confirmed_minute_used':CUTOFF,
              'mode':'postclose_ohlcv_replay_partial_coverage',
              'technical_definition':'10:00-14:49的200个交易分钟等分5段，每段40分钟量取中位数；末段/首段>=1.5且4次相邻比较至少3次上升。14:40-14:49分钟创新高，后续分钟回落>=0.05%且最低价不得跌破对应VWAP',
              'limitations':['候选池取自收盘后涨幅排行榜，不是14:30原始候选池，可能漏掉14:50符合但15:00不在预筛区间的股票',
                             '分钟数据无交易所逐条签名时间戳；仅按历史交易日和分钟标签截断',
                             '采用1分钟OHLC近似新高与回踩，无法证明突破那一分钟内的价格事件顺序',
                             '流通股本为回放时获取的基础数据，必须核对其updated_date是否晚于决策时点',
                             '单一历史1分钟K线重建VWAP；历史来源可回查，但收盘涨幅榜仅目标当日有效',
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
                    updated = str(r.get('updated_date') or '')
                    if updated and updated > DATE:
                        raise ValueError(f'流通股本资料更新时间晚于回放日: {updated}')
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
                    print(f"基础通过 {code} {r.get('name')} 代理条件: "+(', '.join(tech['failed_conditions']) or '全部通过'),flush=True)
                except Exception as e:
                    report['errors'].append(f'{code}: {type(e).__name__}: {str(e)[:160]}')
            if args.max_minutes and (time.monotonic()-begun)/60>args.max_minutes:
                report['errors'].append('用户设置的最长运行时限已到，回放不完整');break
    report['candidates'].sort(key=lambda x:(x['technical_proxy_pass'],x['basic']['volume_ratio']),reverse=True)
    report['summary']={'basic_screen_count':len(report['candidates']),
                       'minute_proxy_pass_count':sum(x['technical_proxy_pass'] for x in report['candidates']),
                       'strict_14_50_count':0,'reason':'收盘后涨幅候选池偏差及分钟级历史回放限制；不属于14:50真实全市场选股',
                       'elapsed_seconds':round(time.monotonic()-begun,2), 'errors':len(report['errors'])}
    path=OUT / 'replay_2026-11-03_ohlcv.json'
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
