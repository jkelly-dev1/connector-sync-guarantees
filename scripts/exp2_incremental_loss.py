"""EXPERIMENT 2: does incremental sync actually catch everything?

    python3 scripts/exp2_incremental_loss.py

The headline experiment and the reason the repository exists.

"We do incremental sync on a modified-since timestamp" is the most common
ingestion design in the industry. This runs one against a world whose every
change is known, and counts what it misses.

The answer key is the mutation timeline. The harness knows every change and
the simulated instant it occurred, so "did the connector see it" is a
measurement. Losses are grouped BY KIND, because the aggregate hides the
finding: a single accuracy figure cannot distinguish random noise from one
entire class of change that no amount of tuning will ever recover.

The mitigations, ablated one at a time, in the order a real team reaches for
them:

    tiebreaker      order by (last_modified, id) so paging is safe at all
    inclusive       use >= at the boundary instead of >
    overlap         re-read a window before the watermark
    deletes API     consult the dedicated deletes endpoint

The prediction, recorded before the run: incremental sync on a modified-since
watermark catches every change. Expect it REFUTED, and expect the losses to
sort into kinds rather than spreading evenly: SILENT_UPDATE unrecoverable at
any setting, DELETE unrecoverable without the deletes call, and the in-flight
and clock-skew losses shrinking but not vanishing as the overlap grows.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lab
from connector.sync import (SyncConfig, attribute_wrong_records,
                            score, score_by_mutation_kind)
from sim import world as W

PREDICTION = {
    "claim": "incremental sync on a modified-since watermark observes every "
             "change that happened",
}

# How often the connector runs, in simulated seconds. Five minutes is a
# realistic incremental cadence for a CRM integration.
INTERVAL_SECONDS = 300

# The cumulative ladder of mitigations. Each row adds one to the row above, so
# the delta between two rows is attributable to exactly one change.
LADDER = [
    ("naive", dict()),
    ("+tiebreaker", dict(use_tiebreaker=True)),
    ("+inclusive_bound", dict(use_tiebreaker=True, inclusive_bound=True)),
    ("+overlap_60s", dict(use_tiebreaker=True, inclusive_bound=True,
                          overlap_seconds=60.0)),
    ("+overlap_300s", dict(use_tiebreaker=True, inclusive_bound=True,
                           overlap_seconds=300.0)),
    ("+deletes_api", dict(use_tiebreaker=True, inclusive_bound=True,
                          overlap_seconds=300.0, use_deletes_api=True)),
]

# The overlap sweep, run with everything else already correct, so the curve
# measures the overlap alone.
OVERLAPS = [0.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0]


def run_one(vendor_name, config, until):
    clock, vendor, lim, conn = lab.build_run(vendor_name, strategy="token_bucket",
                                             config=config)
    conn.backfill()
    conn.run(until=until, interval_seconds=INTERVAL_SECONDS)
    s = score(conn.store, vendor)
    by_kind = score_by_mutation_kind(conn.store, vendor, vendor.timeline,
                                     up_to=clock.now())
    blame = attribute_wrong_records(conn.store, vendor, vendor.timeline,
                                    up_to=clock.now())
    return {
        "wrong_records_by_cause": blame,
        "config": config.as_dict(),
        "passes": conn.passes,
        "simulated_seconds": round(clock.now(), 1),
        "calls": vendor.calls,
        "quota_share_used": vendor.usage()["quota_share_used"],
        "records_returned": vendor.records_returned,
        "redundant_upserts": conn.store.redundant_upserts,
        "score": s,
        "by_mutation_kind": by_kind,
    }


def main():
    until = W.TIMELINE_SECONDS
    out = {"interval_seconds": INTERVAL_SECONDS,
           "timeline_seconds": until,
           "vendors": {}}

    for vendor_name in ("atlas", "beacon"):
        print("=== %s ===" % vendor_name)
        ladder_rows = []
        for label, kwargs in LADDER:
            # The deletes API only exists on atlas; beacon archives instead,
            # so the equivalent mitigation there is to scan for the flag.
            k = dict(kwargs)
            if vendor_name == "beacon" and k.pop("use_deletes_api", False):
                k["scan_archived"] = True
                label = "+scan_archived"
            r = run_one(vendor_name, SyncConfig(**k), until)
            r["label"] = label
            ladder_rows.append(r)
            s = r["score"]
            print("  %-18s accuracy %.4f  missing %4d  stale %4d  ghost %4d"
                  % (label, s["accuracy"], s["missing"], s["stale"],
                     s["ghost_records"]))

        print("  overlap sweep (tiebreaker + inclusive bound already on)")
        overlap_rows = []
        for ov in OVERLAPS:
            cfg = SyncConfig(use_tiebreaker=True, inclusive_bound=True,
                             overlap_seconds=ov)
            r = run_one(vendor_name, cfg, until)
            r["overlap_seconds"] = ov
            overlap_rows.append(r)
            s = r["score"]
            print("    overlap %6.0fs  accuracy %.4f  wrong %4d  "
                  "redundant reads %6d  calls %5d"
                  % (ov, s["accuracy"], s["wrong"], r["redundant_upserts"],
                     r["calls"]))

        out["vendors"][vendor_name] = {"ladder": ladder_rows,
                                       "overlap_sweep": overlap_rows}

    # ---- The verdict -------------------------------------------------------
    # the prediction is scored on the best configuration, not the naive one.
    # Refuting it with the naive setup would be trivial and would prove
    # nothing; the claim worth testing is whether a CORRECTLY TUNED watermark
    # sync catches everything.
    best = out["vendors"]["atlas"]["ladder"][-1]
    silent = best["by_mutation_kind"].get("SILENT_UPDATE", {})
    blame = best["wrong_records_by_cause"]
    held = best["score"]["wrong"] == 0

    print()
    print("best configuration on atlas: %d changes still wrong"
          % best["score"]["wrong"])
    print("  SILENT_UPDATE missed %d of %d (%.1f%%)"
          % (silent.get("missed", 0), silent.get("happened", 0),
             100.0 * silent.get("miss_rate", 0.0)))
    print("  each wrong record blamed on its LATEST unreflected change:")
    for kind, n in sorted(blame["attributed_to_kind"].items(),
                          key=lambda kv: -kv[1]):
        print("    %-18s %3d of %d wrong records"
              % (kind, n, blame["wrong_records"]))

    out["prediction"] = dict(
        PREDICTION,
        best_config_wrong_records=best["score"]["wrong"],
        silent_update_miss_rate=silent.get("miss_rate"),
        wrong_records_by_cause=blame,
        verdict="held" if held else "REFUTED")
    print("prediction: %s" % out["prediction"]["verdict"])

    lab.write_result("exp2_incremental_loss", out)
    print("wrote results/exp2_incremental_loss.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
