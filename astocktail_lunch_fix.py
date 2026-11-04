#!/usr/bin/env python3
"""V2.0.0 -> V2.0.1: guarded lunch-boundary normalization and doctor checks.

Run from the deployed project with its .venv Python. No network, signals,
credentials, timers, or data directories are changed. Tests run before install.
"""
import argparse
from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HELPER = '''def normalize_lunch_bars(raw):
    """Adapter for observed eltdx 3.3.2 1m lunch-boundary labels.

    Only remap a single 13:00 bar when 11:30 is absent and unique 11:29
    and 13:01 neighbors exist on the same date. Never fill prices/volume,
    shift other bars, or mutate the source rows. Downstream validation
    still rejects any other missing/duplicate required minute.
    """
    rows = list(raw)
    dates = {}
    for i, row in enumerate(rows):
        ts = parse_time(row['time'])
        dates.setdefault(ts.date().isoformat(), {}).setdefault(ts.strftime('%H:%M'), []).append((i, ts))
    changes = []
    for day, minutes in dates.items():
        if '11:30' in minutes or '13:00' not in minutes:
            continue
        if any(len(minutes.get(t, [])) != 1 for t in ('11:29', '13:00', '13:01')):
            raise ValueError('ambiguous_lunch_boundary_' + day)
        i, ts = minutes['13:00'][0]
        if ts.second != 0 or ts.microsecond != 0:
            raise ValueError('invalid_lunch_boundary_seconds_' + day)
        original = rows[i]['time']
        replacement = ts.replace(hour=11, minute=30).isoformat()
        rows[i] = {**rows[i], 'time': replacement,
                   'source_time': original, 'time_adjustment': 'lunch_1300_to_1130'}
        changes.append({'date': day, 'source_time': str(original), 'time': replacement})
    return rows, changes


'''

REGRESSION = r'''import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import astocktail_observer as o
from test_observer import fixture, FakeMarket, DAY

def wire(rows):
    return [{**r, 'time': r['time'].replace('T11:30:', 'T13:00:')} for r in rows]

class LunchTests(unittest.TestCase):
    def test_stock_prices_amounts_volumes_and_signals_preserved(self):
        v = list(fixture())
        expected = o.evaluate_candidate(*v[:5], DAY, v[5])
        v[3], v[4] = wire(v[3]), wire(v[4])
        before = copy.deepcopy(v)
        actual = o.evaluate_candidate(*v[:5], DAY, v[5])
        for key in ('basic', 'common_flags', 'common_metrics', 'variants', 'groups', 'ma5', 'ma10'):
            self.assertEqual(expected[key], actual[key], key)
        self.assertEqual(v, before)
        self.assertEqual(len(actual['minute_time_adjustments']['stock']), 1)
        self.assertEqual(len(actual['minute_time_adjustments']['index']), 1)
        points = o.minute_points(v[3], DAY)
        self.assertEqual(len(points), 229)
        self.assertEqual(sum(p['volume'] for p in points), sum(r['volume_lots'] for r in v[3]))
        self.assertEqual(sum(p['amount'] for p in points), sum(r['amount'] for r in v[3]))

    def test_normal_data_unchanged(self):
        rows = fixture()[3]
        normalized, changes = o.normalize_lunch_bars(rows)
        self.assertEqual(rows, normalized)
        self.assertEqual(changes, [])

    def test_true_missing_1130_is_not_filled(self):
        v = list(fixture())
        v[3] = [r for r in v[3] if 'T11:30:' not in r['time']]
        with self.assertRaises(ValueError):
            o.minute_points(v[3], DAY)

    def test_other_missing_minute_still_rejected(self):
        rows = wire(fixture()[3])
        rows = [r for r in rows if 'T10:17:' not in r['time']]
        with self.assertRaises(ValueError):
            o.minute_points(rows, DAY)

    def test_missing_1449_still_rejected(self):
        with self.assertRaises(ValueError):
            o.minute_points(wire(fixture()[3])[:-1], DAY)

    def test_duplicate_1300_rejected(self):
        rows = wire(fixture()[3])
        rows.append(next(r for r in rows if 'T13:00:' in r['time']))
        with self.assertRaises(ValueError):
            o.minute_points(rows, DAY)

    def test_missing_neighbor_rejected(self):
        rows = [r for r in wire(fixture()[3]) if 'T13:01:' not in r['time']]
        with self.assertRaises(ValueError):
            o.normalize_lunch_bars(rows)

    def test_already_has_1130_no_remap(self):
        rows = fixture()[3]
        extra = {**rows[0], 'time': DAY + 'T13:00:00+08:00'}
        normalized, changes = o.normalize_lunch_bars(rows + [extra])
        self.assertEqual(changes, [])
        self.assertEqual(normalized[-1], extra)

    def test_multiple_days_keep_date_and_prices(self):
        a = wire(fixture()[3])
        b = [{**r, 'time': r['time'].replace(DAY, '2026-11-02')} for r in a]
        rows, changes = o.normalize_lunch_bars(b + a)
        self.assertEqual(len(changes), 2)
        self.assertEqual([r['close'] for r in rows], [r['close'] for r in b + a])

    def test_nonzero_seconds_rejected(self):
        rows = wire(fixture()[3])
        rows = [{**r, 'time': r['time'].replace('T13:00:00', 'T13:00:01')} for r in rows]
        with self.assertRaises(ValueError):
            o.normalize_lunch_bars(rows)

    def test_doctor_accepts_normalized_lunch_and_records_it(self):
        class Market(FakeMarket):
            def call(self, op, deadline, **kw):
                data = super().call(op, deadline, **kw)
                if op == 'bars' and kw['period'] == '1m':
                    data['bars'] = wire(data['bars'])
                return data
        with tempfile.TemporaryDirectory() as d, patch.object(o, 'Market', Market), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(o.doctor(Path(d)), 0)
            report = json.loads(next(Path(d).glob('observer_data/doctor/*/doctor.json')).read_text())
            self.assertEqual(report['minute_validation'][0]['required'], 229)
            self.assertEqual(len(report['minute_validation'][0]['time_adjustments']), 1)

    def test_doctor_rejects_other_missing_minute(self):
        class Market(FakeMarket):
            def call(self, op, deadline, **kw):
                data = super().call(op, deadline, **kw)
                if op == 'bars' and kw['period'] == '1m':
                    data['bars'] = [r for r in wire(data['bars']) if 'T10:17:' not in r['time']]
                return data
        with tempfile.TemporaryDirectory() as d, patch.object(o, 'Market', Market), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(o.doctor(Path(d)), 2)

if __name__ == '__main__':
    unittest.main()
'''

def patch_source(source):
    if "VERSION = 'rank-observer-2.0.1'" in source:
        raise SystemExit('已经是2.0.1，无需重复修复。')
    replacements = [
        ("VERSION = 'rank-observer-2.0.0'", "VERSION = 'rank-observer-2.0.1'"),
        ("    'cutoff': '14:49',", "    'minute_time_policy': 'eltdx332_guarded_1300_to_1130_v1',\n    'cutoff': '14:49',"),
        ('def minute_points(raw, day):\n    points=[]', HELPER + 'def minute_points(raw, day):\n    raw, _time_adjustments = normalize_lunch_bars(raw)\n    points=[]'),
        ('def index_series(raw,day):\n    idx={}', 'def index_series(raw,day):\n    raw, _time_adjustments = normalize_lunch_bars(raw)\n    idx={}'),
        ("            'evaluated_at':stamp(),'data_cutoff':f'{day}T14:49:00+08:00'}", "            'minute_time_adjustments': {\n                'stock': [x for x in normalize_lunch_bars(stock_raw)[1] if x['date'] == day],\n                'index': [x for x in normalize_lunch_bars(index_raw)[1] if x['date'] == day]},\n            'evaluated_at':stamp(),'data_cutoff':f'{day}T14:49:00+08:00'}"),
        ("        result['minute_endpoints']='stock_and_index_schema_passed'", "        result['minute_endpoints']='stock_and_index_schema_and_available_session_passed'"),
        ("                for field in ('close','high','low','amount','volume_lots'): number(bar[field])\n        result['minute_endpoints']", "                for field in ('close','high','low','amount','volume_lots'): number(bar[field])\n            normalized, adjustments = normalize_lunch_bars(bars)\n            session = result['latest_index_session']\n            times = [parse_time(b['time']).strftime('%H:%M') for b in normalized\n                     if parse_time(b['time']).date().isoformat() == session]\n            limit = (now() - timedelta(minutes=1)).strftime('%H:%M') if session == now().date().isoformat() else '14:49'\n            required = [t for t in EXPECTED if t <= min(limit, '14:49')]\n            missing = [t for t in required if t not in times]\n            if not required or missing or any(times.count(t) != 1 for t in required):\n                raise ValueError('doctor_incomplete_minutes_' + symbol + ':' + ','.join(missing[:8]))\n            if kind is None and len(required) == len(EXPECTED):\n                minute_points(bars, session)\n            result.setdefault('minute_validation', []).append({\n                'code': symbol, 'session': session, 'required': len(required),\n                'through': required[-1], 'time_adjustments': adjustments})\n        result['minute_endpoints']"),
    ]
    for old, new in replacements:
        if source.count(old) != 1:
            raise SystemExit('原文件与V2.0.0不一致，未修改。无法定位: ' + old[:90])
        source = source.replace(old, new, 1)
    compile(source, 'astocktail_observer.py', 'exec')
    return source

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--check-only', action='store_true', help='测试补丁但不安装')
    args = parser.parse_args()
    root = args.root.resolve()
    target = root / 'astocktail_observer.py'
    original = target.read_bytes()
    source = patch_source(original.decode('utf-8'))
    tests = ['test_observer.py', 'test_live_v2.py']
    if any(not (root / name).is_file() for name in tests):
        raise SystemExit('缺少原测试文件，请在完整V2项目目录运行。未修改。')
    with tempfile.TemporaryDirectory(prefix='astocktail-lunch-test-') as temp:
        temp_path = Path(temp)
        (temp_path / target.name).write_text(source, encoding='utf-8')
        for name in tests:
            shutil.copyfile(root / name, temp_path / name)
        (temp_path / 'test_lunch_boundary.py').write_text(REGRESSION, encoding='utf-8')
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        result = subprocess.run([sys.executable, '-m', 'unittest', 'test_observer',
                                 'test_live_v2', 'test_lunch_boundary', '-v'],
                                cwd=temp, env=env, timeout=90)
        if result.returncode:
            raise SystemExit('测试未通过，部署文件未修改。')
    if args.check_only:
        print('60项测试通过；仅检查，未修改部署文件。')
        return
    if target.read_bytes() != original:
        raise SystemExit('测试期间原文件发生变化，未安装。')
    backup = root / (target.name + '.before_lunch_fix_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    with backup.open('xb') as f:
        f.write(original)
    backup.chmod(target.stat().st_mode & 0o777)
    fd, temporary = tempfile.mkstemp(prefix='.lunch-fix-', dir=root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(source)
        os.chmod(temporary, target.stat().st_mode & 0o777)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print('已更新: rank-observer-2.0.1')
    print('原脚本备份:', backup)
    print('新脚本SHA256:', hashlib.sha256(target.read_bytes()).hexdigest())
    print('未修改定时器、密钥或observer_data。下一步运行doctor。')

if __name__ == '__main__':
    main()
