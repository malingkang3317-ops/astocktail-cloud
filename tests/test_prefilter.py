import unittest
import pandas as pd
from strategy import prefilter_snapshot_frame, baseline_check


class TestPrefilter(unittest.TestCase):
    def test_loose_includes_near_threshold(self):
        df=pd.DataFrame([{'代码':'600000','名称':'样例','最新价':10, '涨跌幅':2.5,
                          '量比':0.9, '换手率':4.0, '流通市值':4800000000}])
        loose,n=prefilter_snapshot_frame(df,relaxed=True)
        strict,m=prefilter_snapshot_frame(df,relaxed=False)
        self.assertEqual((n,m),(1,0))
        self.assertFalse(baseline_check(loose.iloc[0].to_dict()))

    def test_excludes_other_markets(self):
        df=pd.DataFrame([{'代码':'830000','名称':'样例','最新价':10,'涨跌幅':4,
                          '量比':2, '换手率':6, '流通市值':8000000000}])
        _,n=prefilter_snapshot_frame(df,relaxed=True)
        self.assertEqual(n,0)

if __name__=='__main__': unittest.main()
