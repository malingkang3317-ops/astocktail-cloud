#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AStockTail: B/C tail shapes x trend filters, offline cached research.

Inputs:
  replay_reports/five_days_abc/abc_report.json
  replay_reports/five_days/cache/daily/{code}.json
  replay_reports/five_days/cache/minutes/{code}.json
Outputs (separate, never overwrites existing strategy):
  replay_reports/five_days_trend/trend_daily_summary.csv
  replay_reports/five_days_trend/trend_overall_summary.csv
  replay_reports/five_days_trend/trend_candidates.csv
  replay_reports/five_days_trend/trend_report.json

All signals use completed 1m bars through 14:49. Trend MA calculation
uses only prior completed daily closes plus the signal-day 14:49 price.
Next trading day OHLC is used ONLY for outcome analysis; missing data
is left missing, never coded as zero. No network/notifications/orders.
"""
import argparse
import csv
import json
import math
import statistics
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

VERSIONS = ('B', 'C')
FILTERS = {
    'none': '不加趋势过滤',
    'ma5_gt_ma10': 'MA5 > MA10',
    'price_gt_ma20': '14:49价格 > MA20',
    'ma20_rising': 'MA20(14:49) > 前一交易日MA20',
}
HEADER = ['day','shape','filter','eligible_base','shape_all_pass','trend_available',
          'trend_pass','selected','next_day_n','next_day_close_mean_pct',
          'next_day_close_median_pct','next_day_close_win_pct',
          'next_day_close_worst_pct','next_day_open_mean_pct',
          'next_day_low_mean_pct','next_day_low_worst_pct',
          'next_day_high_mean_pct','next_open_to_low_mean_pct',
          'next_open_to_low_worst_pct','outcome_missing','trend_missing']


def finite_num(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def stats(values):
    vals = [float(v) for v in values if finite_num(v) is not None]
    if not vals:
        return {'n': 0, 'mean': None, 'median': None, 'win_rate': None,
                'min': None, 'max': None}
    return {'n':len(vals),'mean':round(statistics.mean(vals),4),
            'median':round(statistics.median(vals),4),
            'win_rate':round(100*sum(v>0 for v in vals)/len(vals),2),
            'min':round(min(vals),4),'max':round(max(vals),4)}


def day_key(bar):
    return str(bar.get('time',''))[:10]


def daily_data(path):
    if not path.is_file():
        raise FileNotFoundError(f'缺少日K缓存: {path}')
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw,list):
        raise ValueError(f'日K缓存格式错误: {path}')
    out = {}
    for b in raw:
        d = day_key(b)
        try: date.fromisoformat(d)
        except ValueError: continue
        row = {k:finite_num(b.get(k)) for k in ('open','high','low','close')}
        if any(v is None or v<=0 for v in row.values()):
            continue
        if row['high'] < max(row['open'],row['close'],row['low']) or row['low'] > min(row['open'],row['close']):
            continue
        if d in out: raise ValueError(f'日K重复交易日 {d}: {path}')
        out[d] = row
    return out


def signal_price(path, day):
    if not path.is_file():
        raise FileNotFoundError(f'缺少分钟缓存: {path}')
    raw = json.loads(path.read_text(encoding='utf-8'))
    matches = [b for b in raw if str(b.get('time',''))[:10]==day and
               str(b.get('time',''))[11:16]=='14:49']
    if len(matches)!=1:
        raise ValueError(f'{day} 14:49分钟缓存缺失/重复 ({len(matches)})')
    price = finite_num(matches[0].get('close'))
    if price is None or price<=0: raise ValueError('14:49价格无效')
    return price


def compute_trend(bars,day,price):
    """MA(today partial) uses 14:49 price instead of final daily close.
    Prior day MA20 uses 20 fully completed closes before the signal date.
    """
    previous=sorted((d,r['close']) for d,r in bars.items() if d<day)
    if len(previous)<20:
        return {'available':False,'reason':f'均线需要此前20个完整交易日，只有{len(previous)}个',
                'values':{},'flags':{f:None for f in FILTERS if f!='none'}}
    c=[v for _,v in previous]
    ma5=(sum(c[-4:])+price)/5
    ma10=(sum(c[-9:])+price)/10
    ma20=(sum(c[-19:])+price)/20
    ma20_previous=sum(c[-20:])/20
    values={'ma5':round(ma5,5),'ma10':round(ma10,5),
            'ma20_partial':round(ma20,5),
            'ma20_prior_full':round(ma20_previous,5),
            'price_1449':price}
    return {'available':True,'reason':None,'values':values,
            'flags':{'ma5_gt_ma10':ma5>ma10,
                     'price_gt_ma20':price>ma20,
                     'ma20_rising':ma20>ma20_previous}}


def following_trade_day(d, available_days):
    """Only permit next day if it matches the known five-day schedule;
    for last signal date, next-day returns remain unknown by design.
    """
    # Use a strict known consecutive trading-day list: avoid silently jumping
    # over missing future sessions and treating a later day as the next day.
    order=['2026-10-28','2026-10-29','2026-10-30','2026-11-02','2026-11-03']
    if d not in order or order.index(d)==len(order)-1: return None
    target=order[order.index(d)+1]
    return target if target in available_days else None


def compute_outcomes(bars, day, entry, cached_expected):
    nextday=following_trade_day(day,bars)
    if not nextday:
        return {'available':False,'next_day':None,'reason':'缺少明确的下一交易日完整日K或超出五日观察期',
                'metrics':{}}
    r=bars[nextday]
    # Cross-check: old A/B/C report stores the close/14:49 return;
    # adjusted daily prices might not be compatible with raw minute prices.
    calculated=(r['close']/entry-1)*100
    expected=finite_num(cached_expected)
    if expected is None:
        return {'available':False,'next_day':nextday,
                'reason':'前版报告没有次日收益记录，避免自行补造不一致结果', 'metrics':{}}
    if abs(calculated-expected)>0.35:
        return {'available':False,'next_day':nextday,
                'reason':f'日K与原报告收益不一致: {calculated:.3f}% vs {expected:.3f}%',
                'metrics':{}}
    o,h,l,c=(r[k] for k in ('open','high','low','close'))
    metrics={
        'next_day_open_pct_vs_1449':100*(o/entry-1),
        'next_day_high_pct_vs_1449':100*(h/entry-1),
        'next_day_low_pct_vs_1449':100*(l/entry-1),
        'next_day_close_pct_vs_1449':calculated,
        'next_day_open_to_low_pct':100*(l/o-1),
        'next_day_open_to_high_pct':100*(h/o-1),
        'next_day_open_to_close_pct':100*(c/o-1),
    }
    return {'available':True,'next_day':nextday,'reason':None,
            'metrics':{k:round(v,4) for k,v in metrics.items()},
            'ohlc':r}


def summarize(rows,shape,flt,day=None):
    all_rows=[r for r in rows if day is None or r['date']==day]
    shape_rows=[r for r in all_rows if r['variants'].get(shape,{}).get('all_technical_pass') is True]
    selected=[r for r in shape_rows if flt=='none' or r['trend']['flags'].get(flt) is True]
    outcomes=[r['next_day']['metrics'] for r in selected if r['next_day']['available']]
    s={k:stats(x[k] for x in outcomes) for k in (
        'next_day_close_pct_vs_1449','next_day_open_pct_vs_1449',
        'next_day_low_pct_vs_1449','next_day_high_pct_vs_1449',
        'next_day_open_to_low_pct')}
    return {'base':len(all_rows),'shape_all_pass':len(shape_rows),
            'trend_available':sum(r['trend']['available'] for r in shape_rows),
            'trend_pass':sum(r['trend']['flags'].get(flt) is True for r in shape_rows) if flt!='none' else len(shape_rows),
            'selected':len(selected),
            'trend_missing':sum(not r['trend']['available'] for r in shape_rows) if flt!='none' else 0,
            'outcome_missing':sum(not r['next_day']['available'] for r in selected),
            'metrics':s,
            'selected_stocks':[{'date':r['date'],'code':r['code'],'name':r['name'],
                     'trend_pass':r['trend']['flags'].get(flt) if flt!='none' else True,
                     'next_day':r['next_day']} for r in selected]}


def tabular(day,shape,flt,s):
    close=s['metrics']['next_day_close_pct_vs_1449']; op=s['metrics']['next_day_open_pct_vs_1449']
    low=s['metrics']['next_day_low_pct_vs_1449']; high=s['metrics']['next_day_high_pct_vs_1449']
    gaplow=s['metrics']['next_day_open_to_low_pct']
    return [day,shape,flt,s['base'],s['shape_all_pass'],s['trend_available'],s['trend_pass'],
            s['selected'],close['n'],close['mean'],close['median'],close['win_rate'],close['min'],
            op['mean'],low['mean'],low['min'],high['mean'],gaplow['mean'],gaplow['min'],
            s['outcome_missing'],s['trend_missing']]


def write_csv(path,header,items):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(header);w.writerows(items)


def run(root,out):
    source=root/'replay_reports'/'five_days_abc'/'abc_report.json'
    cache=root/'replay_reports'/'five_days'/'cache'
    if not source.is_file():raise FileNotFoundError(f'需要先运行A/B/C回测，缺少: {source}')
    report=json.loads(source.read_text(encoding='utf-8'))
    original=report.get('candidate_details')
    if not isinstance(original,list):raise ValueError('abc_report.json中没有candidate_details，请检查脚本版本')
    if not original:raise ValueError('A/B/C回测报告没有候选记录')
    out.mkdir(parents=True,exist_ok=True)
    rows=[];errors=[];memo={}
    for x in original:
        day=str(x['date']);code=str(x['code'])
        variants=x.get('variants',{})
        if not any(variants.get(v,{}).get('all_technical_pass') for v in VERSIONS):
            # No trade under B/C: still needed for daily denominator, but
            # unnecessary to load its daily cache.
            rows.append({**x,'trend':{'available':False,'reason':'B/C形态未全通过',
                                     'values':{},'flags':{k:None for k in FILTERS if k!='none'}},
                         'next_day':{'available':False,'next_day':None,'reason':'无B/C入选信号','metrics':{}}})
            continue
        try:
            if code not in memo:
                memo[code]=daily_data(cache/'daily'/f'{code}.json')
            bars=memo[code]
            entry=signal_price(cache/'minutes'/f'{code}.json',day)
            old_price=finite_num(x.get('basic',{}).get('price'))
            if old_price is None or abs(entry-old_price)>0.02:
                raise ValueError(f'分钟价{entry}与A/B/C报告价{old_price}不匹配')
            trend=compute_trend(bars,day,entry)
            outcome=compute_outcomes(bars,day,entry,x.get('next_day_close_return_pct_vs_1449'))
            if not trend['available']:errors.append({'date':day,'code':code,'type':'trend','reason':trend['reason']})
            if not outcome['available'] and day!='2026-11-03':errors.append({'date':day,'code':code,'type':'outcome','reason':outcome['reason']})
            rows.append({**x,'entry_1449':entry,'trend':trend,'next_day':outcome})
        except Exception as exc:
            reason=f'{type(exc).__name__}: {exc}'
            errors.append({'date':day,'code':code,'type':'cache','reason':reason})
            rows.append({**x,'trend':{'available':False,'reason':reason,'values':{},
                  'flags':{k:None for k in FILTERS if k!='none'}},
                  'next_day':{'available':False,'reason':reason,'next_day':None,'metrics':{}}})
    days=sorted({x['date'] for x in rows})
    daily=[];overall=[]
    for d in days:
        for v in VERSIONS:
            for f in FILTERS:
                daily.append(tabular(d,v,f,summarize(rows,v,f,d)))
    results={}
    for v in VERSIONS:
        results[v]={}
        for f in FILTERS:
            s=summarize(rows,v,f)
            results[v][f]=s
            overall.append(tabular('ALL',v,f,s))
    write_csv(out/'trend_daily_summary.csv',HEADER,daily)
    write_csv(out/'trend_overall_summary.csv',HEADER,overall)
    detail_cols=['date','code','name','B_all','C_all','signal_price_1449',
       'trend_available','ma5','ma10','ma20_partial','ma20_prior_full',
       'ma5_gt_ma10','price_gt_ma20','ma20_rising','next_day',
       'next_open_pct_vs_1449','next_high_pct_vs_1449','next_low_pct_vs_1449',
       'next_close_pct_vs_1449','next_open_to_low_pct','data_warning']
    detail=[]
    for r in rows:
        if not any(r['variants'].get(v,{}).get('all_technical_pass') for v in VERSIONS):continue
        tr=r['trend'];o=r['next_day'];mv=tr.get('values',{});met=o.get('metrics',{})
        detail.append([r['date'],r['code'],r.get('name'),
             *[r['variants'][v]['all_technical_pass'] for v in VERSIONS],
             r.get('entry_1449'),tr['available'],
             *[mv.get(k) for k in ('ma5','ma10','ma20_partial','ma20_prior_full')],
             *[tr['flags'].get(k) for k in ('ma5_gt_ma10','price_gt_ma20','ma20_rising')],
             o.get('next_day'),
             *[met.get(k) for k in ('next_day_open_pct_vs_1449','next_day_high_pct_vs_1449',
                      'next_day_low_pct_vs_1449','next_day_close_pct_vs_1449','next_day_open_to_low_pct')],
             '; '.join(filter(None,[tr.get('reason'),o.get('reason')]))])
    write_csv(out/'trend_candidates.csv',detail_cols,detail)
    final={'metadata':{
       'source':str(source),'mode':'B/C x 3 trend filters, historical proxy research, OFFLINE',
       'days':days,'cutoff':'14:49 completed minute bar',
       'shape_rules':'Exactly as existing A/B/C report, unchanged',
       'basic_conditions':'Exactly as V3/ABC report, unchanged',
       'filters':FILTERS,
       'trend_definition':{
           'ma5_gt_ma10':'5-day partial MA at 14:49 > 10-day partial MA at 14:49',
           'price_gt_ma20':'14:49 price > 20-day partial MA at 14:49',
           'ma20_rising':'20-day partial MA at 14:49 > prior completed trading day 20-day MA',
       },
       'risks':[
          'Historical shares / daily turnover proxies not independently verified',
          'Past universe missing symbols; 5 trading days too few for predictive validation',
          'Daily OHLC cannot determine intraday outcome order or achievable fills',
          'Reference returns from 14:49 are not executable; entry may be next open with slippage',
          'MA uses 14:49 partial price, never same-day 15:00 daily close',
          'Next day high/low are optimistic/pessimistic extrema, not simultaneously tradable',
          'Outcome data available only through 2026-11-03; missing outcomes remain NULL',
          'Daily OHLC checked against existing next-day close returns; mismatch excluded',
          'No transaction costs, suspension, ex-rights, limit-up/down execution assumptions',
          'No 20–60 day extension without new verified historical data',
       ],
       'source_proxy_complete':report.get('metadata',{}).get('complete',False),
       'base_records':len(rows),'bc_signal_records':len(detail),
       'warnings':len(errors),
       'no_network':True,'no_notifications':True,'no_live_strategy_changes':True,
       },'daily_summary':daily,'overall':results,
       'candidate_details':[r for r in rows if any(r['variants'].get(v,{}).get('all_technical_pass') for v in VERSIONS)],
       'errors':errors}
    (out/'trend_report.json').write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding='utf-8')
    print('=== B/C + 均线趋势过滤 离线对照回测 ===')
    for v in VERSIONS:
        print(f'\n{v} 策略:')
        for f in FILTERS:
            s=results[v][f]
            st=s['metrics']['next_day_close_pct_vs_1449']
            low=s['metrics']['next_day_low_pct_vs_1449']
            print(f'  {f}: 入选={s["selected"]} 次日收益样本={st["n"]} '
                  f'均值={st["mean"]} 胜率={st["win_rate"]} '
                  f'次日最低价相对14:49最差={low["min"]} '
                  f'趋势缺失={s["trend_missing"]} 收益缺失={s["outcome_missing"]}')
    print(f'\n源候选记录={len(rows)} B/C信号记录={len(detail)} 缓存警告={len(errors)}')
    print('输出目录:',out)
    return final


def main():
    ap=argparse.ArgumentParser(description='AStockTail B/C vs three trend filters cached-only backtest')
    ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    ap.add_argument('--out',type=Path)
    args=ap.parse_args()
    root=args.root.resolve()
    out=(args.out or root/'replay_reports'/'five_days_trend').resolve()
    run(root,out)

if __name__=='__main__':main()
