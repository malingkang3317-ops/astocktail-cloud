import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime,timedelta
import time
import astocktail_observer as o

DAY='2026-11-03'


def hang_worker(pipe):
    time.sleep(60)

def fixture():
    previous=[]; dt=datetime(2026,11,2,tzinfo=o.TZ)
    while len(previous)<30:
        if dt.weekday()<5: previous.append(dt.date().isoformat())
        dt-=timedelta(days=1)
    previous=sorted(previous)
    daily={d:{'open':9.6,'high':9.7,'low':9.2,'close':9.3+i*.3/29,'volume_lots':100000.0} for i,d in enumerate(previous)}
    calendar={**daily,DAY:{'open':9.7,'high':10.1,'low':9.6,'close':10,'volume_lots':400000}}
    stock=[]; index=[]
    for i,hm in enumerate(o.EXPECTED):
        price=9.7+.3*i/228; volume=2500 if i>=189 else 1500
        stock.append({'time':f'{DAY}T{hm}:00+08:00','open':price,'close':price,'high':price+.02,
                      'low':price-.01,'amount':volume*100*9.6,'volume_lots':volume})
        index.append({'time':f'{DAY}T{hm}:00+08:00','open':10,'close':10,'high':10,'low':10,
                      'amount':100000,'volume_lots':100})
    quote={'code':'002531','exchange':'sz','last_price':10,'pre_close_price':9.6,'total_hand':400000,'amount':4e8}
    item={'code':'sz002531','name':'fixture','quote':quote,'rank':100,'observed_at':f'{DAY}T14:49:00+08:00',
          'pre':{'volume_ratio':3,'turnover_pct':7}}
    profile={'data':{'code':'002531','exchange':'sz','name':'fixture','circulating_shares':6e8},'collected_at':f'{DAY}T14:40:00+08:00'}
    return item,daily,profile,stock,index,calendar


class FakeMarket:
    def __init__(self,*a): self.events=[]
    def close(self): pass
    def call(self,op,deadline,**kw):
        item,daily,profile,stock,index,calendar=fixture()
        if op=='version': return {'eltdx':'fixture'}
        if op=='rank': return {'records':[item['quote']]}
        if op=='profile': return {'rows':[profile['data']]}
        if op=='bars':
            if kw['period']=='day':
                data=calendar if kw.get('kind')=='index' else {**daily,DAY:calendar[DAY]}
                return {'bars':[{'time':d+'T15:00:00+08:00',**v} for d,v in data.items()]}
            return {'bars':index if kw.get('kind')=='index' else stock}
        raise ValueError(op)


class RulesTests(unittest.TestCase):
    def test_fixture_passes_three_groups(self):
        r=o.evaluate_candidate(*fixture()[:5],DAY,fixture()[5])
        self.assertTrue(all(r['groups'].values()),r)
    def test_future_minutes_cannot_change_signal(self):
        values=list(fixture()); a=o.evaluate_candidate(*values[:5],DAY,values[5])
        for arr in (values[3],values[4]):
            arr.append({'time':DAY+'T15:00:00+08:00','close':999,'high':999,'low':1,'volume_lots':999999,'amount':1})
        b=o.evaluate_candidate(*values[:5],DAY,values[5])
        self.assertEqual(a['basic'],b['basic']);self.assertEqual(a['groups'],b['groups'])
    def test_today_daily_close_not_used_for_ma(self):
        values=list(fixture()); a=o.evaluate_candidate(*values[:5],DAY,values[5])
        values[1][DAY]={'open':999,'high':999,'low':999,'close':999,'volume_lots':999}
        b=o.evaluate_candidate(*values[:5],DAY,values[5])
        self.assertEqual((a['ma5'],a['ma10']),(b['ma5'],b['ma10']))
    def test_missing_1449_rejected(self):
        vals=list(fixture()); vals[3].pop()
        with self.assertRaises(ValueError):o.evaluate_candidate(*vals[:5],DAY,vals[5])
    def test_duplicate_minute_rejected(self):
        vals=list(fixture()); vals[3].append(vals[3][-1])
        with self.assertRaises(ValueError):o.evaluate_candidate(*vals[:5],DAY,vals[5])
    def test_stale_day_rejected(self):
        vals=list(fixture());vals[3]=[{**b,'time':b['time'].replace(DAY,'2026-11-02')} for b in vals[3]]
        with self.assertRaises(ValueError):o.evaluate_candidate(*vals[:5],DAY,vals[5])
    def test_index_missing_minute_rejected(self):
        vals=list(fixture()); vals[4].pop(100)
        with self.assertRaises(ValueError):o.evaluate_candidate(*vals[:5],DAY,vals[5])
    def test_preclose_mismatch_rejected(self):
        vals=list(fixture()); vals[0]['quote']['pre_close_price']=9.1
        with self.assertRaises(ValueError):o.evaluate_candidate(*vals[:5],DAY,vals[5])
    def test_missing_prior_session_rejected(self):
        vals=list(fixture()); vals[1].pop(sorted(vals[1])[-2])
        with self.assertRaises(ValueError):o.evaluate_candidate(*vals[:5],DAY,vals[5])
    def test_ma_missing_does_not_discard_baseline_C(self):
        vals=list(fixture()); vals[1]={d:b for d,b in sorted(vals[1].items())[-10:]}
        r=o.evaluate_candidate(*vals[:5],DAY,vals[5])
        self.assertTrue(r['groups']['C']); self.assertFalse(r['groups']['C_MA5_GT_MA10'])
        self.assertIsNone(r['ma5_gt_ma10'])
    def test_no_same_bar_pullback(self):
        points=[{'time':'14:00','high':10,'low':9.9,'vwap':9.8},
                {'time':'14:49','high':11,'low':10,'vwap':9.8}]
        self.assertTrue(o.shape_events(points,'B')['setup'])
        self.assertFalse(o.shape_events(points,'B')['pullback'])
    def test_naive_timestamp_is_shanghai(self):
        vals=list(fixture()); vals[3]=[{**b,'time':b['time'].replace('+08:00','')} for b in vals[3]]
        r=o.evaluate_candidate(*vals[:5],DAY,vals[5]); self.assertTrue(r['groups']['C'])
    def test_infinite_value_rejected(self):
        vals=list(fixture());vals[3][0]['amount']=float('inf')
        with self.assertRaises(ValueError):o.evaluate_candidate(*vals[:5],DAY,vals[5])


class FlowTests(unittest.TestCase):
    def test_network_deadline_kills_stalled_worker(self):
        with patch.object(o,'_network_worker',hang_worker):
            market=o.Market(); start=time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    market.call('rank',time.monotonic()+.25,category='沪深A股')
                self.assertIsNone(market.proc)
                self.assertLess(time.monotonic()-start,3)
            finally:market.close()
    def test_live_flow_and_rerun_do_not_change_signal(self):
        clock=[datetime(2026,11,3,14,39,30,tzinfo=o.TZ)]
        def advance(target):
            if target>clock[0]:clock[0]=target
        with tempfile.TemporaryDirectory() as d,patch.object(o,'Market',FakeMarket),patch.object(o,'now',side_effect=lambda:clock[0]),patch.object(o,'wait_until',side_effect=advance),contextlib.redirect_stdout(io.StringIO()):
            root=Path(d)
            self.assertEqual(o.run_observation(root,'live',False),0)
            path=root/'observer_data/live'/DAY/'signals.json'
            original=path.read_bytes();report=json.loads(original)
            self.assertEqual(report['status'],'ok');self.assertEqual(len(report['pushed']),1)
            self.assertEqual(report['condition_pass_counts']['denominator_evaluated'],1)
            self.assertEqual(o.run_observation(root,'live',False),0)
            self.assertEqual(original,path.read_bytes())
    def test_postclose_update_keeps_signal_immutable(self):
        clock=[datetime(2026,11,3,14,39,30,tzinfo=o.TZ)]
        def advance(target):clock[0]=max(clock[0],target)
        with tempfile.TemporaryDirectory() as d,patch.object(o,'Market',FakeMarket),patch.object(o,'now',side_effect=lambda:clock[0]),patch.object(o,'wait_until',side_effect=advance),contextlib.redirect_stdout(io.StringIO()):
            root=Path(d);o.run_observation(root,'live',False)
            path=root/'observer_data/live'/DAY/'signals.json';original=path.read_bytes()
            clock[0]=datetime(2026,11,3,16,tzinfo=o.TZ)
            self.assertEqual(o.update_outcomes(root),0)
            self.assertEqual(original,path.read_bytes())
            outcome=json.loads((root/'observer_data/outcomes/latest.json').read_text())
            self.assertEqual(outcome['records'][DAY+'|sz002531']['horizons']['1']['status'],'pending')
            self.assertTrue((root/'observer_data/outcomes/summary.csv').exists())
    def test_rank_fetches_beyond_first_page(self):
        class Pages:
            def call(self,op,deadline,**kw):
                start=kw['start'];self.last=start
                changes=[7]*80 if start==0 else [4,2]
                return {'records':[{'code':str(600000+start+i),'last_price':10*(1+c/100),
                                    'pre_close_price':10,'total_hand':100} for i,c in enumerate(changes)]}
        m=Pages(); hits,stats=o.scan_ranks(m,time.monotonic()+10)
        self.assertEqual(stats['pages'],2);self.assertEqual(len(hits),1)
        self.assertEqual(stats['stop_reason'],'below_band')
    def test_rank_bad_sort_marked(self):
        class Pages:
            def call(self,*a,**kw):return {'records':[{'code':'600001','last_price':10.4,'pre_close_price':10},
                                                    {'code':'600002','last_price':10.8,'pre_close_price':10}]}
        hits,stats=o.scan_ranks(Pages(),time.monotonic()+10)
        self.assertEqual(stats['stop_reason'],'invalid_sort_order')
    def test_rank_deadline_stops_requests(self):
        hits,stats=o.scan_ranks(None,time.monotonic()-1)
        self.assertEqual(stats['pages'],0);self.assertEqual(stats['stop_reason'],'time_limit')
    def test_analysis_has_100_stock_cap(self):
        pool={str(i):{'code':str(i),'pre':{'volume_ratio':1,'turnover_pct':1}} for i in range(130)}
        result,stats=o.final_evaluate(None,pool,{}, {},DAY,{},time.monotonic()-1)
        self.assertEqual(stats['capacity_skipped'],30);self.assertEqual(stats['timeout_skipped'],100)
        self.assertEqual(len(stats['selected_for_analysis']),100)
    def test_fixed_ranking_tie_breaker(self):
        rows=[{'code':c,'pre':{'volume_ratio':2,'turnover_pct':5}} for c in ['sz002531','sh600000']]
        self.assertEqual(sorted(rows,key=o.order_key)[0]['code'],'sh600000')
    def test_signal_write_is_immutable(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'signals.json';o.write_json(p,{'a':1},immutable=True)
            with self.assertRaises(FileExistsError):o.write_json(p,{'a':2},immutable=True)
            self.assertEqual(json.loads(p.read_text()),{'a':1})
    def test_rehearsal_complete_flow_no_notification(self):
        with tempfile.TemporaryDirectory() as d,patch.object(o,'Market',FakeMarket),patch.object(o,'now',return_value=datetime(2026,11,3,16,tzinfo=o.TZ)),contextlib.redirect_stdout(io.StringIO()):
            rc=o.run_observation(Path(d),'rehearsal',True)
            reports=list(Path(d).glob('observer_data/rehearsal/*/signals.json'))
            self.assertEqual(rc,0);self.assertEqual(len(reports),1)
            report=json.loads(reports[0].read_text())
            self.assertEqual(report['status'],'rehearsal_ok');self.assertEqual(report['group_counts']['C'],1)
            self.assertEqual(report['pushed'],[])
            notice=json.loads(next(reports[0].parent.glob('notification_*.json')).read_text())
            self.assertEqual(notice['status'],'disabled')
    def test_late_live_run_never_creates_signals(self):
        with tempfile.TemporaryDirectory() as d,patch.object(o,'now',return_value=datetime(2026,11,3,15,tzinfo=o.TZ)),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(o.run_observation(Path(d),'live'),2)
            self.assertFalse(list(Path(d).glob('observer_data/live/*/signals.json')))
    def test_notification_timeout_body_omits_candidates(self):
        report={'date':DAY,'started_at':'x','status':'timed_out','finished_at':'x','mode':'live',
                'pushed':[{'code':'SECRET_CANDIDATE'}]}
        title,body=o.notification_text(report);self.assertNotIn('SECRET_CANDIDATE',body)
    def test_env_file_does_not_execute_shell(self):
        import os
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{},clear=True):
            p=Path(d)/'env';p.write_text('UNRELATED=notloaded\nSERVERCHAN_SENDKEY="$(echo hi)"\n')
            o.load_env(p)
            self.assertNotIn('UNRELATED',os.environ);self.assertEqual(os.environ['SERVERCHAN_SENDKEY'],'$(echo hi)')


class OutcomesTests(unittest.TestCase):
    def signal(self):return {'date':'2026-10-30','signal_price_1449':10,'basic':{'previous_close':9}}
    def bars(self):return {d:{'close':c,'open':c,'high':c+1,'low':c-1} for d,c in
                          [('2026-10-29',9),('2026-10-30',10),('2026-11-02',11),('2026-11-03',12),('2026-11-04',13)]}
    def test_next_session_skips_weekend_via_calendar(self):
        bars=self.bars();r=o.outcome_for(self.signal(),bars,list(bars),1)
        self.assertEqual(r['target_day'],'2026-11-02');self.assertAlmostEqual(r['return_pct'],10)
    def test_three_session_return(self):
        bars=self.bars();r=o.outcome_for(self.signal(),bars,list(bars),3)
        self.assertAlmostEqual(r['return_pct'],30)
    def test_missing_session_not_relabelled_next_day(self):
        bars=self.bars();calendar=list(bars);del bars['2026-11-02']
        r=o.outcome_for(self.signal(),bars,calendar,1)
        self.assertEqual(r['status'],'missing_session_or_suspended');self.assertIsNone(r['return_pct'])
    def test_five_sessions_not_mature_is_pending(self):
        bars=self.bars();r=o.outcome_for(self.signal(),bars,list(bars),5)
        self.assertEqual(r['status'],'pending');self.assertIsNone(r['return_pct'])
    def test_revised_price_basis_excluded(self):
        bars=self.bars();bars['2026-10-29']['close']=8
        self.assertEqual(o.outcome_for(self.signal(),bars,list(bars),1)['status'],'historical_price_basis_changed')
    def test_empty_stats_not_zero(self):
        self.assertEqual(o.stats_values([])['n'],0);self.assertIsNone(o.stats_values([])['mean_pct'])


if __name__=='__main__':unittest.main(verbosity=2)
