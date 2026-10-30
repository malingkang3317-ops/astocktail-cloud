"""Strict, fail-closed 8-rule A-share tail-screen. No orders or investment advice."""
from __future__ import annotations
import json
import os
import time
import traceback
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

TZ = ZoneInfo('Asia/Shanghai')
ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
CONFIG = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))


def now_cn():
    return datetime.now(TZ)


def log(msg):
    print(f'[{now_cn().isoformat(timespec="seconds")}] {msg}', flush=True)


def get_ak():
    import akshare as ak
    return ak


def cutoff_for(day):
    h, m = map(int, CONFIG['cutoff'].split(':'))
    return datetime.combine(day, dt_time(h, m), TZ)


def trading_day(ak, d):
    cal = ak.tool_trade_date_hist_sina()
    dates = set(pd.to_datetime(cal['trade_date']).dt.date)
    return d in dates


def num(s):
    return pd.to_numeric(s, errors='coerce')


def pref_candidates(ak, observed_at):
    """Snapshot itself has no exchange timestamp; mark acquisition timestamp separately."""
    snap = ak.stock_zh_a_spot_em()
    if snap is None or snap.empty:
        raise RuntimeError('A股快照为空')
    needed = ['代码','名称','最新价','涨跌幅','量比','换手率','流通市值']
    if any(c not in snap.columns for c in needed):
        raise RuntimeError(f'行情字段缺失: {set(needed)-set(snap.columns)}')
    d = snap.copy()
    for c in needed[2:]:
        d[c] = num(d[c])
    d['代码'] = d['代码'].astype(str).str.zfill(6)
    d = d[d['代码'].str.match(r'^(60|68|00|30)\d{4}$')]
    d = d[(d['涨跌幅']>=CONFIG['min_change_pct']) & (d['涨跌幅']<=CONFIG['max_change_pct']) &
          (d['量比']>CONFIG['min_volume_ratio']) &
          (d['换手率']>=CONFIG['min_turnover_pct']) & (d['换手率']<=CONFIG['max_turnover_pct']) &
          (d['流通市值']>=CONFIG['min_float_market_cap_yuan']) & (d['流通市值']<=CONFIG['max_float_market_cap_yuan'])]
    d = d.sort_values(['量比','换手率'], ascending=False)
    return d.head(CONFIG['max_candidates_to_verify']).to_dict('records'), len(d), observed_at.isoformat(timespec='seconds')


def bars_asof(df, day, cut):
    if df is None or df.empty or '时间' not in df:
        raise ValueError('1分钟数据为空或缺时间字段')
    d = df.copy()
    d['时间'] = pd.to_datetime(d['时间'], errors='coerce')
    d = d[(d['时间'].dt.date == day) & (d['时间'] <= cut.replace(tzinfo=None))].sort_values('时间').drop_duplicates('时间')
    for c in ['开盘','收盘','最高','最低','成交量','成交额']:
        if c not in d.columns:
            raise ValueError(f'1分钟字段不存在: {c}')
        d[c] = num(d[c])
    d = d.dropna(subset=['收盘','最高','最低','成交量','成交额'])
    d = d[(d['成交量'] >= 0) & (d['成交额'] >= 0) & (d['收盘'] > 0)]
    if len(d) < CONFIG['min_bars_required']:
        raise ValueError(f'分钟K线不足({len(d)})')
    last = d['时间'].iloc[-1]
    lag = (cut.replace(tzinfo=None) - last).total_seconds()/60
    if lag < 0 or lag > CONFIG['max_final_data_lag_minutes']:
        raise ValueError(f'分钟行情不够新: {last}, lag={lag:.1f}min')
    return d.reset_index(drop=True)


def index_code(stock_code):
    # Shanghai: SSE Composite; Shenzhen: SZ Component; ChiNext: ChiNext; STAR: STAR50
    if stock_code.startswith('68'):
        return '000688'
    if stock_code.startswith('30'):
        return '399006'
    if stock_code.startswith('60'):
        return '000001'
    return '399001'


def five_min_volume_condition(d):
    """Recent stepped demand, no isolated one-minute volume blast."""
    x = d.copy()
    x = x[(x['时间'].dt.strftime('%H:%M') >= '13:30') &
          (x['时间'].dt.strftime('%H:%M') <= '14:49')].copy()
    if len(x) < 60 or x['成交量'].sum() <= 0:
        return False, '下午窗口缺失'
    share = x['成交量'].max() / x['成交量'].sum()
    if share > CONFIG['max_single_minute_volume_fraction']:
        return False, f'单分钟成交量脉冲占比{share:.1%}'
    x['bucket'] = x['时间'].dt.floor('5min')
    blocks = x.groupby('bucket')['成交量'].sum()
    if len(blocks) < 10:
        return False, '5分钟成交量区间不足'
    # Last 4 complete-ish blocks (14:25..14:44) versus previous 4.
    base = blocks[(blocks.index.strftime('%H:%M') >= '13:50') & (blocks.index.strftime('%H:%M') <= '14:10')]
    tail = blocks[(blocks.index.strftime('%H:%M') >= '14:20') & (blocks.index.strftime('%H:%M') <= '14:40')]
    if len(base)<4 or len(tail)<4 or base.mean() <= 0:
        return False, '成交量基准窗口不全'
    growing = int((tail.diff().dropna() > 0).sum())
    passed = tail.mean() > base.mean()*1.12 and growing >= CONFIG['min_stepwise_blocks']
    return bool(passed), f'后段/前段成交量={tail.mean()/base.mean():.2f}x，递增段数={growing}，单分钟占比={share:.1%}'


def evaluate(stock, raw_stock, raw_index, day, cut):
    s = bars_asof(raw_stock, day, cut)
    idx = bars_asof(raw_index, day, cut)
    # align by minute and compare time-matched relative performance from 09:31
    aligned = s[['时间','收盘']].merge(idx[['时间','收盘']],on='时间',how='inner',suffixes=('_stock','_index'))
    if len(aligned) < CONFIG['min_bars_required']:
        raise ValueError('指数与个股分钟K线无法完整对齐')
    stock_ret = aligned['收盘_stock'].iloc[-1]/aligned['收盘_stock'].iloc[0]-1
    idx_ret = aligned['收盘_index'].iloc[-1]/aligned['收盘_index'].iloc[0]-1
    stronger = stock_ret > idx_ret
    # Exact intraday VWAP: volumes in 100 shares, amounts in yuan -> volume*100.
    vol = s['成交量'].cumsum() * 100
    vwap = s['成交额'].cumsum()/vol.where(vol>0)
    if vwap.isna().any() or (vwap<=0).any():
        raise ValueError('无法计算可靠分时VWAP')
    vwap_fraction = float((s['收盘']>=vwap).mean())
    above = vwap_fraction >= CONFIG['vwap_above_fraction']
    vol_ok, volume_note = five_min_volume_condition(s)
    # 14:43-14:48 an intraday high, followed by a visible pullback before 14:50
    prior = s[s['时间'].dt.strftime('%H:%M') < CONFIG['high_window_start']]
    window = s[(s['时间'].dt.strftime('%H:%M') >= CONFIG['high_window_start']) &
               (s['时间'].dt.strftime('%H:%M') <= CONFIG['high_window_end'])]
    if prior.empty or window.empty:
        raise ValueError('14:45突破窗口缺失')
    peak = window['最高'].max()
    peak_i = window['最高'].idxmax()
    after = s.loc[peak_i+1:]
    new_high = bool(peak > prior['最高'].max())
    if after.empty:
        pullback_ok = False
        note='创新高后没有可验证的回踩分钟'
    else:
        peak_p = float(peak)
        low = float(after['最低'].min())
        depth = (peak_p-low)/peak_p
        pullback_ok = (depth >= CONFIG['min_pullback_fraction'] and
                       depth <= CONFIG['max_pullback_fraction'] and
                       bool((after['最低']>=vwap.loc[after.index]*0.999).all()))
        note=f'突破={peak_p:.2f},回踩低={low:.2f},幅度={depth:.2%},未破VWAP={bool((after["最低"]>=vwap.loc[after.index]*0.999).all())}'
    flags = {'成交量阶梯放大':vol_ok,'强于指数':bool(stronger),'均价线上方':bool(above),
             '14:45附近创新高':new_high,'回踩均价线不破':bool(pullback_ok)}
    detail = {'代码':stock['代码'],'名称':stock['名称'],'最新价':float(stock['最新价']),
              '涨跌幅':float(stock['涨跌幅']),'量比':float(stock['量比']),
              '换手率':float(stock['换手率']),'流通市值亿元':round(float(stock['流通市值'])/1e8,2),
              '行情快照获取时间':stock.get('采集时间',''),'分钟行情截止':str(s['时间'].iloc[-1]),
              '指数':index_code(stock['代码']),'相对指数超额涨幅百分点':round((stock_ret-idx_ret)*100,2),
              '均价线上方比例':round(vwap_fraction,3), '成交量依据':volume_note,'尾盘突破回踩':note,
              '规则验证':flags,'完全符合':all(flags.values())}
    return detail


def notify(summary):
    key = os.environ.get('SERVERCHAN_SENDKEY','').strip()
    token = os.environ.get('TELEGRAM_BOT_TOKEN','').strip()
    chat = os.environ.get('TELEGRAM_CHAT_ID','').strip()
    rows = summary.get('selected',[])
    if summary['status'] == 'closed':
        return
    message = (f"A股尾盘筛选 {summary['date']}\n状态: {summary['status']}\n"
               f"严格符合: {len(rows)}只\n" + '\n'.join(
                   f"{x['代码']} {x['名称']} {x['涨跌幅']:+.2f}% 量比{x['量比']:.2f} 换手{x['换手率']:.2f}%" for x in rows)
               + '\n' + summary.get('message','') + '\n仅供研究，非投资建议')
    if key:
        try:
            r=requests.post(f'https://sctapi.ftqq.com/{key}.send',data={'title':'A股尾盘筛选','desp':message},timeout=15);r.raise_for_status()
        except Exception as e: log(f'Server酱通知失败: {e}')
    if token and chat:
        try:
            r=requests.post(f'https://api.telegram.org/bot{token}/sendMessage',data={'chat_id':chat,'text':message},timeout=15);r.raise_for_status()
        except Exception as e: log(f'Telegram通知失败: {e}')


def save(result):
    dest=DATA / f"{result['date']}.json"
    dest.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (DATA/'latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    log(f'报告已保存 {dest}')
    notify(result)


def run(manual=False):
    ak=get_ak()
    today=now_cn().date()
    if not trading_day(ak,today):
        log('沪深休市，直接结束；不发送选股通知')
        return
    cutoff=cutoff_for(today)
    result={'date':str(today),'cutoff':cutoff.isoformat(),'generated_at':now_cn().isoformat(),
            'status':'not_verified','message':'','selected':[],'verified':[], 'rejections':[],
            'definitions':{'量比':'快照接口字段量比，约为截至取数时点每分钟成交量与过去5日同期均量之比；源接口未提供精确交易所时间戳',
                          '均价线':'累计成交额 / (累计成交量（手）×100)；需确认源成交量单位为手',
                          '价格快照时间':'仅记录HTTP数据获取时刻，无法证明交易所源数据毫秒级实时'}}
    try:
        # Only execute at a bounded valid decision window. Never run late and call it 14:50.
        start=cutoff-timedelta(minutes=8)
        if not manual:
            if now_cn()<start:
                wait=(start-now_cn()).total_seconds()
                if wait>300: raise RuntimeError('任务启动过早，拒绝长时间等待')
                time.sleep(max(0,wait))
            if now_cn()>cutoff+timedelta(minutes=CONFIG['max_run_lateness_minutes']):
                raise RuntimeError('工作流启动过晚，无法声称14:50实时选股')
        log('交易日确认；拉取预筛快照')
        pref, total, acquired=pref_candidates(ak, now_cn())
        log(f'快照通过基础条件 {total} 只；可验证前 {len(pref)} 只')
        # Need latest snapshot at cutoff. This process is often NOT fast enough: fail closed.
        if not manual and now_cn()<cutoff:
            time.sleep((cutoff-now_cn()).total_seconds())
        snap_time=now_cn()
        if snap_time>cutoff+timedelta(minutes=CONFIG['max_run_lateness_minutes']):
            raise RuntimeError('14:50最终验证窗口已错过（快照查询超时）')
        final, final_total, _=pref_candidates(ak,snap_time)
        chosen_codes={x['代码'] for x in pref} | {x['代码'] for x in final}
        final=[x for x in final if x['代码'] in chosen_codes]
        # Snapshot has no per-stock exchange timestamp: it is an explicit provenance limitation.
        result['snapshot_captured_at']=snap_time.isoformat(timespec='seconds')
        cache={}
        for stock in final:
            if len(result['selected'])>=3: break
            code=stock['代码']; idx_code=index_code(code)
            if now_cn()>cutoff+timedelta(minutes=CONFIG['max_run_lateness_minutes']):
                result['rejections'].append({'代码':code,'原因':'超出行情时效窗口，停止验证'})
                break
            try:
                stock['采集时间']=snap_time.isoformat(timespec='seconds')
                if idx_code not in cache:
                    cache[idx_code]=ak.index_zh_a_hist_min_em(symbol=idx_code,period='1',
                         start_date=f'{today} 09:30:00', end_date=f'{today} 14:50:00')
                raw=ak.stock_zh_a_hist_min_em(symbol=code,period='1',
                         start_date=f'{today} 09:30:00', end_date=f'{today} 14:50:00',adjust='')
                detail=evaluate(stock,raw,cache[idx_code],today,cutoff)
                result['verified'].append(detail)
                if detail['完全符合']:
                    result['selected'].append(detail)
                else:
                    result['rejections'].append({'代码':code,'原因':str([k for k,v in detail['规则验证'].items() if not v])})
            except Exception as exc:
                result['rejections'].append({'代码':code,'原因':str(exc)})
        result['status']='verified' if result['selected'] else 'no_match_or_unverified'
        result['message']=f"基础条件{final_total}只，完整验证{len(result['verified'])}只；源无交易所逐股快照时间戳，非严格交易所实时证明。"
        result['generated_at']=now_cn().isoformat()
    except Exception as exc:
        result['status']='data_unavailable'
        result['message']=str(exc)
        result['generated_at']=now_cn().isoformat()
        log(traceback.format_exc())
    save(result)


if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser(); ap.add_argument('--manual',action='store_true',help='仅用于连接/规则测试，非14:50实盘信号')
    args=ap.parse_args()
    run(manual=args.manual)
