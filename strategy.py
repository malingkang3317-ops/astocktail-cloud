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


def within(value, low, high):
    return low <= value <= high


def prefilter_snapshot_frame(snap, relaxed=True):
    """Broad candidate discovery from a SINGLE market snapshot; not final verification."""
    if snap is None or snap.empty:
        raise RuntimeError('A股快照为空')
    needed = ['代码','名称','最新价','涨跌幅','量比','换手率','流通市值']
    missing = set(needed) - set(snap.columns)
    if missing:
        raise RuntimeError(f'行情字段缺失: {missing}')
    d = snap.copy()
    for col in needed[2:]:
        d[col] = num(d[col])
    d['代码'] = d['代码'].astype(str).str.zfill(6)
    d = d[d['代码'].str.fullmatch(r'(60|68|00|30)\d{4}') == True]
    if relaxed:
        lo, hi = CONFIG['pre_min_change_pct'], CONFIG['pre_max_change_pct']
        vr = CONFIG['pre_min_volume_ratio']
        to_lo, to_hi = CONFIG['pre_min_turnover_pct'], CONFIG['pre_max_turnover_pct']
        cap_lo, cap_hi = CONFIG['pre_min_float_market_cap_yuan'], CONFIG['pre_max_float_market_cap_yuan']
    else:
        lo, hi = CONFIG['min_change_pct'], CONFIG['max_change_pct']
        vr = CONFIG['min_volume_ratio']
        to_lo, to_hi = CONFIG['min_turnover_pct'], CONFIG['max_turnover_pct']
        cap_lo, cap_hi = CONFIG['min_float_market_cap_yuan'], CONFIG['max_float_market_cap_yuan']
    d = d[d['涨跌幅'].between(lo, hi) & (d['量比'] > vr) &
          d['换手率'].between(to_lo, to_hi) & d['流通市值'].between(cap_lo, cap_hi)]
    d = d.sort_values(['量比','换手率'], ascending=False)
    return d, len(d)


def pref_candidates(ak, observed_at, relaxed=True):
    """One all-market AKShare fetch. Capture the timestamp AFTER completion."""
    started = now_cn()
    snap = ak.stock_zh_a_spot_em()
    fetched = now_cn()
    d, total = prefilter_snapshot_frame(snap, relaxed=relaxed)
    log(f'全市场快照请求耗时={(fetched-started).total_seconds():.1f}s，宽松候选{total}只，采集结束={fetched.isoformat()}')
    return d.head(CONFIG['max_candidates_to_verify']).to_dict('records'), total, fetched.isoformat(timespec='seconds')


def baseline_check(stock):
    """Check the original snapshot values, do NOT label them final 14:50 values."""
    return bool(
        within(float(stock['涨跌幅']), CONFIG['min_change_pct'], CONFIG['max_change_pct']) and
        float(stock['量比']) > CONFIG['min_volume_ratio'] and
        within(float(stock['换手率']), CONFIG['min_turnover_pct'], CONFIG['max_turnover_pct']) and
        within(float(stock['流通市值']), CONFIG['min_float_market_cap_yuan'], CONFIG['max_float_market_cap_yuan'])
    )

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


def _minute_stock(ak, code, day, cutoff):
    return ak.stock_zh_a_hist_min_em(symbol=code,period='1',
            start_date=f'{day} 09:30:00',end_date=f'{day} 14:50:00',adjust='')


def _minute_index(ak, code, day):
    return ak.index_zh_a_hist_min_em(symbol=code,period='1',
            start_date=f'{day} 09:30:00',end_date=f'{day} 14:50:00')


def run(manual=False):
    ak=get_ak()
    today=now_cn().date()
    if not trading_day(ak,today):
        log('沪深休市，不发选股通知')
        return
    cutoff=cutoff_for(today)
    result={'date':str(today),'cutoff':cutoff.isoformat(), 'generated_at':now_cn().isoformat(),
            'status':'not_verified','message':'','selected':[],'verified':[], 'rejections':[],
            'preselected':[], 'definitions':{
              '量比':'初筛AKShare快照字段；无逐股交易所时间戳，不能证明14:50量比',
              '均价线':'累计成交额/（累计成交量（手）×100），必须确认数据源单位',
              '实时性':'14:50分钟线仅为分钟价量，不能替代14:50量比及换手率的独立核验'}}
    try:
        start=cutoff-timedelta(minutes=20)
        if not manual:
            if now_cn()<start:
                delay=(start-now_cn()).total_seconds()
                if delay>CONFIG['max_wait_before_start_minutes']*60:
                    raise RuntimeError('任务启动过早，超出等待上限')
                time.sleep(max(0,delay))
            if now_cn()>cutoff-timedelta(minutes=CONFIG['minimum_preparation_minutes']):
                raise RuntimeError('启动时间过晚，无法完成预筛和候选跟踪')
        log('交易日确认，获取唯一一次全市场快照并宽松预筛')
        pref,total,acquired=pref_candidates(ak,now_cn(),relaxed=True)
        result['prefilter_captured_at']=acquired
        result['prefilter_count']=total
        result['preselected']=[{'代码':x['代码'],'名称':x['名称']} for x in pref]
        if not pref:
            result['status']='no_prefilter_candidates'
            result['message']='本次全市场快照宽松预筛无候选；此结果不是14:50全市场严格筛选证明'
            return
        # IMPORTANT: never make a second whole-market request. Warm candidate/index data instead.
        idx_cache={}
        now=now_cn()
        if not manual and now<cutoff-timedelta(minutes=5):
            wait=(cutoff-timedelta(minutes=5)-now).total_seconds()
            log(f'等待至14:45进行候选分钟线预取：{wait:.0f}s')
            time.sleep(max(0,wait))
        if not manual and now_cn()>cutoff+timedelta(seconds=CONFIG['final_bar_grace_seconds']):
            raise RuntimeError('预取前已超出最终验证窗口')
        # fetch index once per symbol; prefetch stock bars only for a bounded subset.
        subset=pref[:CONFIG['max_candidate_refresh_count']]
        for stock in subset:
            if not manual and now_cn()>cutoff+timedelta(seconds=CONFIG['final_bar_grace_seconds']):
                break
            code=stock['代码']; idx=index_code(code)
            try:
                if idx not in idx_cache:
                    idx_cache[idx]=_minute_index(ak,idx,today)
                # prefetch is an early API connectivity check, not the final 14:50 bars
                _minute_stock(ak,code,today,cutoff)
            except Exception as exc:
                log(f'候选预取失败 {code}: {exc}')
        if not manual and now_cn()<cutoff:
            time.sleep(max(0,(cutoff-now_cn()).total_seconds()))
        if not manual and now_cn()>cutoff+timedelta(seconds=CONFIG['final_bar_grace_seconds']):
            raise RuntimeError('14:50复核窗口已错过；未取得足够及时的分钟行情')
        # Fetch latest 1-minute bars of candidates, BUT strict base fields are not reverified.
        # Thus result['selected'] must remain empty unless a future authenticated per-stock
        # final snapshot implementation can verify price/change/ratio/turnover/cap at cutoff.
        for stock in subset:
            code=stock['代码']
            if not manual and now_cn()>cutoff+timedelta(seconds=CONFIG['final_bar_grace_seconds']):
                result['rejections'].append({'代码':code,'原因':'超过分钟线补齐等待上限'})
                continue
            try:
                idx=index_code(code)
                stock['采集时间']=acquired
                bars=_minute_stock(ak,code,today,cutoff)
                if idx not in idx_cache:
                    idx_cache[idx]=_minute_index(ak,idx,today)
                detail=evaluate(stock,bars,idx_cache[idx],today,cutoff)
                detail['初筛快照基础条件']=baseline_check(stock)
                detail['14:50基础指标已独立复核']=False
                detail['完全符合']=False
                detail['分钟技术形态通过']=all(detail['规则验证'].values())
                detail['验证说明']='分钟技术形态已评估；14:50量比、换手率、流通市值没有可靠逐股终值，不能声称8项严格全部通过'
                result['verified'].append(detail)
                if detail['分钟技术形态通过'] and detail['初筛快照基础条件']:
                    result['rejections'].append({'代码':code,'原因':'分时技术条件通过，但14:50基础行情字段未独立验证；仅供观察'})
                else:
                    failed=[k for k,v in detail['规则验证'].items() if not v]
                    result['rejections'].append({'代码':code,'原因':'未通过:'+','.join(failed) if failed else '初筛快照基础条件与严格条件不同'})
            except Exception as exc:
                result['rejections'].append({'代码':code,'原因':str(exc)})
        result['status']='partial_verified' if result['verified'] else 'data_unavailable'
        result['message']=(f'唯一全市场快照预筛{total}只；候选复核{len(result["verified"])}只。'
          '由于免费接口缺少可核实的14:50逐股量比/换手率等数据，严格符合0只，不能把技术形态通过等同于8项全部通过。'
          '本方案为效率优先的候选池跟踪，不能保证14:30以后新达标股票均被纳入。')
    except Exception as exc:
        result['status']='data_unavailable'
        result['message']=str(exc)
        log(traceback.format_exc())
    finally:
        result['generated_at']=now_cn().isoformat()
        save(result)


if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser(); ap.add_argument('--manual',action='store_true',help='仅用于连接/规则测试，非14:50实盘信号')
    args=ap.parse_args()
    run(manual=args.manual)
