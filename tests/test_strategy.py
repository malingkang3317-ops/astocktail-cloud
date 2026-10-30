import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo
import pandas as pd
from strategy import bars_asof, index_code, cutoff_for

class TestRules(unittest.TestCase):
    def test_index_mapping(self):
        self.assertEqual(index_code('600000'),'000001')
        self.assertEqual(index_code('300750'),'399006')
        self.assertEqual(index_code('000001'),'399001')
        self.assertEqual(index_code('688001'),'000688')
    def test_cutoff(self):
        c=cutoff_for(date(2026,10,30))
        self.assertEqual((c.hour,c.minute,c.tzinfo.key),(14,50,'Asia/Shanghai'))
    def test_no_fresh_bars(self):
        d=pd.DataFrame({'时间':['2026-10-30 09:31:00'],'开盘':[1],'收盘':[1],'最高':[1], '最低':[1], '成交量':[100],'成交额':[10000]})
        with self.assertRaises(ValueError):
            bars_asof(d,date(2026,10,30),cutoff_for(date(2026,10,30)))
if __name__=='__main__': unittest.main()
