"""EXPERIMENT 1: what a rate limit costs, and what actually fixes it.

    python3 scripts/exp1_rate_limits.py

Four client-side limiter strategies, backfilling the same corpus from the same
vendor, measured in SIMULATED seconds and API calls. Nothing here reads the
wall clock, so every figure is exactly reproducible.

Two scenarios, because there are two kinds of limit and they are not the same
problem:

  RATE-BOUND   a generous daily allowance and a tight per-second ceiling. This
               is a THROUGHPUT problem: pacing decides how long the backfill
               takes, and the strategies should separate.
  CAP-BOUND    a tight daily allowance. This is a CAPACITY problem: the work
               does not fit in today's budget at all, and no amount of pacing
               creates capacity.

The third comparison is the one that matters. Alongside the limiter sweep, the
same backfill is run through three ACCESS PATTERNS: a paged scan, the same scan
followed by a per-record detail fetch (the N+1 that kills real connectors), and
an async bulk export. Tuning a limiter moves a number; changing the access
pattern moves an order of magnitude.

The prediction, recorded before the run: a smarter limiter completes the
backfill faster. Expect it QUALIFIED. True under a per-second ceiling, and
close to meaningless under a daily cap, where every strategy fails at the same
point because the cap is capacity rather than pacing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lab
from sim import limiters, vendors, world as W
from sim.clock import Clock
from connector.sync import Connector, Store, SyncConfig
from sim.vendors import QuotaExhausted

PREDICTION = {
    "claim": "a smarter client-side limiter completes the backfill faster",
}

# RATE-BOUND: the daily allowance is not the constraint; the per-second
# ceiling is. A paged scan at 200 records per page needs ~21 calls, which is
# nowhere near the allowance, so pacing decides everything.
RATE_BOUND = {"daily_allowance": 100000, "per_second_limit": 5.0,
              "page_size": 200}

# CAP-BOUND: the SAME corpus through a small page size, against an allowance
# that cannot cover it. 4,000 records at 20 per page needs ~200 calls and only
# 100 are available. NOTHING a limiter does creates the missing 100 calls.
CAP_BOUND = {"daily_allowance": 100, "per_second_limit": 5.0,
             "page_size": 20}


def make_vendor(name, overrides):
    clock = Clock()
    cls = lab.VENDORS[name]
    v = cls(clock)
    for k, val in overrides.items():
        if k != "page_size":
            setattr(v, k, val)
    return clock, v


def backfill_with(name, strategy, overrides, share=0.5):
    clock, vendor = make_vendor(name, overrides)
    documented = vendor.per_second_limit
    lim = limiters.build(strategy, clock, documented, share=share)
    conn = Connector(vendor, clock, lim, Store(),
                     SyncConfig(page_size=overrides.get("page_size", 200)))
    completed = True
    try:
        conn.backfill()
    except QuotaExhausted:
        completed = False
    u = vendor.usage()
    return {
        "strategy": strategy,
        "completed": completed and not conn.quota_exhausted,
        "records_held": len(conn.store.records),
        "simulated_seconds": round(clock.now(), 2),
        "calls": u["calls"],
        "calls_throttled": u["calls_throttled"],
        "quota_used": u["quota_used"],
        "quota_share_used": u["quota_share_used"],
        "throttle_events": conn.throttle_events,
        "limiter": lim.stats(),
    }


def access_patterns(overrides):
    """Three ways to get the same 4,000 records out of Atlas.

    The point of this table is that the choice of endpoint dominates the
    choice of limiter. A connector that pages a list endpoint and then fetches
    each record for detail issues 200x the calls of one that does not, and no
    limiter strategy recovers that.
    """
    out = []

    # (a) paged scan only
    clock, vendor = make_vendor("atlas", overrides)
    lim = limiters.build("token_bucket", clock, vendor.per_second_limit, share=0.5)
    conn = Connector(vendor, clock, lim, Store(), SyncConfig(page_size=200))
    try:
        conn.backfill()
        ok = True
    except QuotaExhausted:
        ok = False
    out.append({"pattern": "paged_scan", "completed": ok,
                "records": len(conn.store.records),
                "calls": vendor.calls,
                "simulated_seconds": round(clock.now(), 2)})

    # (b) paged scan then a detail fetch per record: the N+1
    clock, vendor = make_vendor("atlas", overrides)
    lim = limiters.build("token_bucket", clock, vendor.per_second_limit, share=0.5)
    conn = Connector(vendor, clock, lim, Store(), SyncConfig(page_size=200))
    ok = True
    try:
        conn.backfill()
        ids = sorted(conn.store.records)
        for rid in ids:
            conn._call(vendor.get_many, [rid])
    except QuotaExhausted:
        ok = False
    out.append({"pattern": "paged_scan_plus_detail_fetch", "completed": ok,
                "records": len(conn.store.records),
                "calls": vendor.calls,
                "simulated_seconds": round(clock.now(), 2)})

    # (c) async bulk export
    clock, vendor = make_vendor("atlas", overrides)
    lim = limiters.build("token_bucket", clock, vendor.per_second_limit, share=0.5)
    conn = Connector(vendor, clock, lim, Store(), SyncConfig())
    ok = True
    try:
        rows = conn._call(vendor.bulk_export)
        for r in rows:
            conn.store.upsert(r, source_version=r.get("version"))
    except QuotaExhausted:
        ok = False
    out.append({"pattern": "bulk_export", "completed": ok,
                "records": len(conn.store.records),
                "calls": vendor.calls,
                "simulated_seconds": round(clock.now(), 2)})
    return out


def main():
    out = {"scenarios": {}, "access_patterns": {}}

    for label, overrides in (("rate_bound", RATE_BOUND),
                             ("cap_bound", CAP_BOUND)):
        print("=== %s (allowance %d, %.0f calls/sec) ==="
              % (label, overrides["daily_allowance"],
                 overrides["per_second_limit"]))
        rows = []
        for strat in limiters.STRATEGIES:
            r = backfill_with("atlas", strat, overrides)
            rows.append(r)
            print("  %-13s %-9s records %5d  calls %5d  throttled %4d  "
                  "sim %8.1fs"
                  % (strat, "complete" if r["completed"] else "INCOMPLETE",
                     r["records_held"], r["calls"], r["calls_throttled"],
                     r["simulated_seconds"]))
        out["scenarios"][label] = {"vendor_config": overrides, "runs": rows}
        print()

    print("=== access patterns, same 4,000 records, generous allowance ===")
    ap = access_patterns(RATE_BOUND)
    for r in ap:
        print("  %-30s %-9s calls %6d  sim %8.1fs"
              % (r["pattern"], "complete" if r["completed"] else "INCOMPLETE",
                 r["calls"], r["simulated_seconds"]))
    out["access_patterns"]["rate_bound"] = ap
    print()

    print("=== access patterns under the tight daily cap ===")
    ap2 = access_patterns(CAP_BOUND)
    for r in ap2:
        print("  %-30s %-9s calls %6d  sim %8.1fs"
              % (r["pattern"], "complete" if r["completed"] else "INCOMPLETE",
                 r["calls"], r["simulated_seconds"]))
    out["access_patterns"]["cap_bound"] = ap2

    # ---- the verdict -------------------------------------------------------
    rate_rows = out["scenarios"]["rate_bound"]["runs"]
    cap_rows = out["scenarios"]["cap_bound"]["runs"]

    # Only completed runs are comparable. A strategy that never finished is
    # not "fast"; comparing its elapsed time against one that did is how a
    # degenerate run turns into a headline.
    finished = {r["strategy"]: r["simulated_seconds"]
                for r in rate_rows if r["completed"]}
    fastest = min(finished, key=finished.get) if finished else None
    slowest = max(finished, key=finished.get) if finished else None
    spread_all = (finished[slowest] / finished[fastest]) if finished else 0.0

    # Two spans, because they answer different questions and one of them was
    # being quoted for the other. "How much does the choice of PACED limiter
    # matter" is a span over the paced strategies. The span over all four is
    # anchored on naive, which is fastest here precisely because it does not
    # pace, so quoting it as the spread "between the paced strategies"
    # attributes naive's recklessness to the tuning of the other three.
    paced_times = {k: v for k, v in finished.items() if k != "naive"}
    spread_rate = ((max(paced_times.values()) / min(paced_times.values()))
                   if paced_times else 0.0)
    did_not_finish = [r["strategy"] for r in rate_rows if not r["completed"]]

    cap_complete = [r["strategy"] for r in cap_rows if r["completed"]]

    scan = [r for r in ap if r["pattern"] == "paged_scan"][0]
    n1 = [r for r in ap if r["pattern"] == "paged_scan_plus_detail_fetch"][0]
    call_ratio = n1["calls"] / max(scan["calls"], 1)

    print()
    print("under a per-second ceiling the PACED strategies spread by %.2fx; "
          "all four including naive spread by %.2fx (%s fastest, %s slowest)"
          % (spread_rate, spread_all, fastest, slowest))
    if did_not_finish:
        print("  did not complete at all: %s" % ", ".join(did_not_finish))
    print("under the daily cap, %d of %d strategies completed"
          % (len(cap_complete), len(cap_rows)))
    print("the N+1 access pattern costs %.0fx the calls of a paged scan"
          % call_ratio)

    # QUALIFIED is the expected verdict: a better limiter genuinely helps
    # under a rate ceiling and does nothing at all under a capacity cap.
    held = spread_rate > 1.2 and len(cap_complete) == len(cap_rows)
    out["prediction"] = dict(
        PREDICTION,
        rate_bound_spread=round(spread_rate, 3),
        rate_bound_spread_including_naive=round(spread_all, 3),
        rate_bound_did_not_finish=did_not_finish,
        fastest_strategy=fastest,
        slowest_strategy=slowest,
        cap_bound_strategies_completed=cap_complete,
        cap_bound_strategies_total=len(cap_rows),
        n_plus_one_call_multiple=round(call_ratio, 1),
        verdict="held" if held else "QUALIFIED")
    print("prediction: %s" % out["prediction"]["verdict"])

    lab.write_result("exp1_rate_limits", out)
    print("wrote results/exp1_rate_limits.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
