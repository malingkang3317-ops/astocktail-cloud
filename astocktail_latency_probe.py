#!/usr/bin/env python3
"""Read-only data-path benchmark; never imports strategy.py or sends notifications.

Run in the existing repository with its .venv Python. This tests components,
not a deployed streaming scanner. Network availability is not data freshness.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

PREFIX = "BENCH_EVENT "
TZ = ZoneInfo("Asia/Shanghai")


def emit(**event):
    print(PREFIX + json.dumps(event, ensure_ascii=False), flush=True)


def timed_call(stage, code, fn, **extra):
    start = time.perf_counter()
    try:
        rows = fn()
        if not isinstance(rows, list) or not rows:
            raise ValueError("empty_or_invalid_rows")
        times = []
        for row in rows:
            dt = datetime.fromisoformat(str(row["time"]))
            dt = dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
            if not math.isfinite(float(row["close"])) or float(row["close"]) <= 0:
                raise ValueError("invalid_close")
            times.append(dt)
        if len(set(times)) != len(times):
            raise ValueError("duplicate_times")
        today = datetime.now(TZ).date()
        labels = {t.strftime("%H:%M") for t in times if t.date() == today}
        expected = {f"{h:02d}:{m:02d}" for h, lo, hi in
                    ((9,31,59),(10,0,59),(11,0,30),(13,1,59),(14,0,49))
                    for m in range(lo, hi+1)}
        emit(stage=stage, code=code, status="ok", seconds=time.perf_counter()-start,
             rows=len(rows), latest=max(times).isoformat(),
             today_through_1449_complete=expected <= labels,
             **extra)
        return True
    except Exception as exc:
        # Exception strings may contain server URLs; keep only the class.
        emit(stage=stage, code=code, status="error", error=type(exc).__name__,
             seconds=time.perf_counter()-start, **extra)
        return False


def snapshot_worker():
    start = time.perf_counter()
    try:
        import akshare as ak
        emit(stage="akshare_import", status="ok", seconds=time.perf_counter()-start)
        start = time.perf_counter()
        frame = ak.stock_zh_a_spot_em()
        required = {"代码", "名称", "最新价", "涨跌幅", "量比", "换手率", "流通市值"}
        valid = frame is not None and not frame.empty and required <= set(frame.columns)
        emit(stage="all_market_snapshot", status="ok" if valid else "invalid",
             seconds=time.perf_counter()-start, rows=0 if frame is None else len(frame),
             note="接口快照无独立行情时点核验；不等同于14:49有效快照")
    except Exception as exc:
        emit(stage="all_market_snapshot", status="error", error=type(exc).__name__,
             seconds=time.perf_counter()-start)


def tdx_worker(root, samples):
    files = sorted((root/"replay_reports/five_days/cache/minutes").glob("*.json"))
    codes = [p.stem for p in files if re.fullmatch(r"(?:sh(?:60|68)|sz(?:00|30))\d{4}", p.stem)]
    source = "fixed_seed_sample_of_existing_minute_cache"
    if not codes:
        codes = ["sz002531", "sz301291", "sh601566"]
        source = "fallback_three_codes_not_representative"
    codes = random.Random(20261104).sample(codes, min(samples, len(codes)))
    emit(stage="sample", status="info", codes=codes, source=source)
    start = time.perf_counter()
    try:
        from eltdx import TdxClient, to_jsonable
        with TdxClient(timeout=8) as client:
            emit(stage="tdx_import_connect", status="ok", seconds=time.perf_counter()-start)
            started = time.perf_counter()
            failures = 0
            # The second pass is a request-size probe, NOT an implemented merge.
            for count, suffix in ((240,"full240"),(5,"delta5")):
                jobs = [(c, False) for c in codes] + [
                    (c, True) for c in ("sh000001","sz399001","sz399006","sh000688")]
                for code, is_index in jobs:
                    if time.perf_counter()-started >= 90 or failures >= 3:
                        emit(stage="tdx_stop", status="budget_or_failures")
                        return
                    kwargs = {"kind":"index"} if is_index else {}
                    ok = timed_call(
                        ("index_" if is_index else "stock_")+suffix, code,
                        lambda: to_jsonable(client.bars.get(
                            code, period="1m", start=0, count=count, **kwargs)).get("bars", []),
                        requested=count)
                    failures = 0 if ok else failures+1
    except Exception as exc:
        emit(stage="tdx_setup", status="error", error=type(exc).__name__,
             seconds=time.perf_counter()-start)


def compute_worker(root):
    """Execute only allowlisted pure functions extracted from existing source.

    No top-level statements or imports from the source are executed. Names below
    are from the inspected V3 and trend scripts. No output/cache writes occur.
    """
    import ast
    sources = [
        ("astocktail_backtest_5days_v3.py", {"points_at", "technical"}),
        ("astocktail_compare_trend.py", {"compute_trend"}),
    ]
    ns = {"datetime":datetime,"TZ":TZ,"CUTOFF":"14:49","statistics":statistics,
          "FILTERS":{"none":"", "ma5_gt_ma10":"", "price_gt_ma20":"", "ma20_rising":""}}
    for filename, names in sources:
        path = root/filename
        if not path.is_file():
            emit(stage="cached_compute", status="missing_script", file=filename)
            return
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        if {n.name for n in nodes} != names:
            emit(stage="cached_compute", status="unsupported_script_version", file=filename)
            return
        # These saved original functions are numerical-only. Reject unexpected
        # source edits that introduce imports, file/network access, or execution.
        allowed_calls = {"float","str","len","list","sorted","set","sum","all","max","min",
                         "range","bool","round","enumerate","ValueError"}
        allowed_attrs = {"fromisoformat","astimezone","strftime","date","isoformat",
                         "get","append","sort","items","values","median"}
        for node in ast.walk(ast.Module(body=nodes, type_ignores=[])):
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
                raise ValueError("unexpected_source_statement")
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                raise ValueError("unexpected_private_attribute")
            if isinstance(node, ast.Call):
                fn = node.func
                if not ((isinstance(fn, ast.Name) and fn.id in allowed_calls) or
                        (isinstance(fn, ast.Attribute) and fn.attr in allowed_attrs)):
                    raise ValueError("unexpected_source_call")
        exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), ns)

    source = root/"replay_reports/five_days_abc/abc_report.json"
    if not source.is_file():
        emit(stage="cached_compute", status="missing_abc_report")
        return
    records = json.loads(source.read_text(encoding="utf-8")).get("candidate_details", [])
    cache = root/"replay_reports/five_days/cache"
    loaded = {}
    loading = processing = 0.0
    good = failed = 0
    for r in records:
        code, day = r["code"], r["date"]
        idx = "sz399006" if code.startswith("sz30") else "sh000688" if code.startswith("sh68") else "sh000001" if code.startswith("sh") else "sz399001"
        try:
            t = time.perf_counter()
            arrays = []
            for sub, symbol in (("minutes",code),("indices",idx),("daily",code)):
                path = cache/sub/(symbol+".json")
                if path not in loaded:
                    loaded[path] = json.loads(path.read_text(encoding="utf-8"))
                arrays.append(loaded[path])
            loading += time.perf_counter()-t
            t = time.perf_counter()
            pts = ns["points_at"](arrays[0], day)
            idx_map = {}
            for b in arrays[1]:
                ts = datetime.fromisoformat(str(b["time"]))
                ts = ts.replace(tzinfo=TZ) if ts.tzinfo is None else ts.astimezone(TZ)
                if ts.date().isoformat() == day and ts.strftime("%H:%M") <= "14:49":
                    idx_map[ts.strftime("%H:%M")] = float(b["close"])
            ns["technical"](pts, idx_map)
            days = {str(b["time"])[:10]:{"close":float(b["close"])} for b in arrays[2]}
            ns["compute_trend"](days, day, pts[-1]["price"])
            processing += time.perf_counter()-t
            good += 1
        except Exception:
            failed += 1
    emit(stage="cached_compute", status="ok" if good and not failed else "partial_or_failed",
         records=len(records), successful=good, failed=failed,
         cache_load_seconds=loading, compute_seconds=processing,
         note="V3技术条件+均线；不是完整B/C流水线；失败记录耗时不进入成功耗时统计")


def parse_events(output):
    events = []
    for line in (output or "").splitlines():
        if line.startswith(PREFIX):
            try:
                events.append(json.loads(line[len(PREFIX):]))
            except (ValueError, TypeError):
                pass
    return events


def run_phase(script, args, timeout):
    start = time.perf_counter()
    try:
        p = subprocess.run([sys.executable, str(script), *args],
                           capture_output=True, text=True, timeout=timeout)
        return {"status":"completed" if p.returncode == 0 else "worker_error",
                "wall_seconds":time.perf_counter()-start,
                "events":parse_events(p.stdout), "returncode":p.returncode}
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {"status":"timeout", "wall_seconds":time.perf_counter()-start,
                "events":parse_events(output)}


def network_summary(events, stage):
    items = [e for e in events if e.get("stage") == stage]
    values = sorted(e["seconds"] for e in items if e.get("status") == "ok")
    return {"attempts":len(items), "success":len(values), "failed":len(items)-len(values),
            "mean_seconds":statistics.mean(values) if values else None,
            "p95_seconds":values[max(0,math.ceil(.95*len(values))-1)] if values else None,
            "today_complete":sum(e.get("today_through_1449_complete",False) for e in items)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--skip-snapshot", action="store_true")
    ap.add_argument("--worker", choices=("snapshot","tdx","compute"), help=argparse.SUPPRESS)
    args = ap.parse_args()
    if not 1 <= args.samples <= 50:
        ap.error("--samples must be between 1 and 50")
    root = args.root.resolve()
    if args.worker:
        try:
            if args.worker == "snapshot": snapshot_worker()
            elif args.worker == "tdx": tdx_worker(root,args.samples)
            else: compute_worker(root)
        except Exception as exc:
            emit(stage=args.worker,status="worker_error",error=type(exc).__name__)
        return
    report = {"started_at_cn":datetime.now(TZ).isoformat(),"samples_requested":args.samples,
              "notification":"NOT_TESTED_NO_MESSAGE_SENT", "end_to_end":"NOT_IMPLEMENTED_OR_MEASURED",
              "phases":{},"script_sha256":{}}
    for name in ("strategy.py","astocktail_backtest_5days_v3.py","astocktail_compare_abc.py","astocktail_compare_trend.py"):
        path = root/name
        if path.is_file():
            report["script_sha256"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    phases = ([] if args.skip_snapshot else [("snapshot",45)]) + [("tdx",110),("compute",40)]
    for name, limit in phases:
        print(f"正在测试 {name}（硬超时 {limit}秒）...", flush=True)
        result = run_phase(Path(__file__).resolve(), ["--worker",name,"--root",str(root),
                          "--samples",str(args.samples)], limit)
        report["phases"][name] = result
        print(f"  {result['status']}，实测墙钟 {result['wall_seconds']:.3f}秒",flush=True)
        for e in result["events"]:
            if e.get("stage") not in ("sample","stock_full240","stock_delta5","index_full240","index_delta5"):
                print(" ",json.dumps(e,ensure_ascii=False),flush=True)
    events = [e for p in report["phases"].values() for e in p["events"]]
    report["network_summary"] = {}
    print("\n=== 分钟行情实测汇总 ===")
    for stage in ("stock_full240","stock_delta5","index_full240","index_delta5"):
        s = network_summary(events, stage)
        report["network_summary"][stage] = s
        print(stage, json.dumps(s,ensure_ascii=False))
    print("\n=== 仅个股串行网络耗时的粗略外推，非全流程预测 ===")
    projections = {}
    for stage in ("stock_full240","stock_delta5"):
        s = report["network_summary"][stage]
        if s["success"] >= 5 and not s["failed"]:
            projections[stage] = {str(n):round(n*s["mean_seconds"],2) for n in (30,100,300)}
            print(stage, projections[stage], "单位秒；不含其他阶段、重试和通知")
        else:
            print(stage,"有效样本不足或有失败，不外推")
    report["network_only_linear_estimates_seconds"] = projections
    report["limitations"] = [
        "非尾盘时段不能证明尾盘服务速度；需在目标时段再次测量",
        "抽样来自旧缓存，不是当天完整候选池；存量缓存样本有选择偏差",
        "delta5仅测试小请求；未验证增量合并、缺口补齐或持续订阅",
        "AKShare快照和eltdx分钟是不同数据源；未核验二者口径一致性",
        "未测试动态预选池、B/C完整算法、通知受理或手机实际送达",
        "不能据本报告保证14:52或14:55前完成；预测不得外推为服务保证"]
    out = root/"replay_reports/latency_probe"
    out.mkdir(parents=True,exist_ok=True)
    dest = out/(datetime.now(TZ).strftime("%Y%m%d_%H%M%S_%f")+".json")
    dest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("\n报告:",dest)
    print("未运行正式策略、未修改行情缓存、未发送通知。全流程按时完成：尚未验证。")


if __name__ == "__main__":
    main()
