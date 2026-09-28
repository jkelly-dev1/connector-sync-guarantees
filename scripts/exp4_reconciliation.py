"""EXPERIMENT 4: what reconciliation costs and what it actually catches.

    python3 scripts/exp4_reconciliation.py

Experiments 2 and 3 leave drift behind: records the connector believes are
current and are not, and records it believes exist and do not. Reconciliation
is the mechanism that makes a platform CORRECT rather than merely fresh, and
its absence is the most common serious defect in integration platforms.

Five strategies, cheapest first:
    none                  the baseline: whatever incremental left wrong
    count_check           compare local and remote counts. One call.
    Partitioned_checksum  count plus a hash of sorted ids per partition; fetch
                          details only where a partition disagrees
    full_id_inventory     fetch every id and diff. Detects deletes definitively.
    Full_field_compare    fetch every record and compare every field. Detects
                          silently wrong VALUES, which no id-level check can.

Measured for each: API calls consumed, drift detected by kind, and drift still
missed. The question is not "which is best", full comparison is always best
and always unaffordable, but where the knee is.

Each strategy is a COST MODEL. It is charged the calls it would make, and
what it detects is read against the answer key (the vendor's own records)
rather than decided from the responses it paid for. The Calls column is what
each approach would cost; it is not a trace of an implementation.

The prediction, recorded before the run: a count check catches most drift.
Expect it REFUTED. A count is blind to equal-and-opposite errors and to every
silent update, and silent updates are precisely what experiment 2 showed
survives a correctly-tuned incremental sync.
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lab
from sim import world as W
from connector.sync import SyncConfig, score

PREDICTION = {
    "claim": "a count check catches most of the drift incremental sync leaves",
}

INTERVAL_SECONDS = 300

# How many partitions the checksum strategy splits the corpus into. More
# partitions localize the damage better and cost more probe calls; this is the
# knob that trades probe cost against repair cost.
PARTITIONS = 40


def drift_report(store, vendor):
    """Every discrepancy between local and upstream, classified.

    Three kinds, and they need different detectors:
      missing   upstream has it, we do not
      ghost     we have it live, upstream does not
      wrong     both have it and the value differs: INVISIBLE to any check
                that only compares identifiers
    """
    truth = {r["id"]: r for r in vendor._live()}
    local = store.live()
    missing = [rid for rid in truth if rid not in local]
    ghost = [rid for rid in local if rid not in truth]
    wrong = [rid for rid in truth
             if rid in local and local[rid].get("version") != truth[rid].get("version")]
    return {"missing": missing, "ghost": ghost, "wrong": wrong}


def partition_of(rid, n):
    return int(hashlib.blake2b(rid.encode(), digest_size=4).hexdigest(), 16) % n


def reconcile(strategy, conn, vendor):
    """Run one strategy. Returns what it found, what it cost, and what it SAW.

    The third value is the count check's doing. Scoring a strategy purely on
    the records it NAMES gives it a detection rate of zero, which is correct
    and is the finding; but written as `found["missing"] |= set()` it is also
    unfalsifiable: the zero comes from a line that can only produce zero, not
    from a run. Reporting the signal separately from the names keeps the zero
    and makes it a measurement.
    """
    calls_before = vendor.calls
    truth = {r["id"]: r for r in vendor._live()}
    local = conn.store.live()
    found = {"missing": set(), "ghost": set(), "wrong": set()}
    signal = {}

    if strategy == "none":
        pass

    elif strategy == "count_check":
        remote_n = conn._call(vendor.count)
        # A count tells you a number, not which records. Even when it differs
        # it names nothing, and when equal-and-opposite errors cancel it does
        # not even differ. It is a smoke alarm, not a diagnosis: it fires here,
        # since the counts really are unequal, and it still adds nothing to
        # `found`, because there is nothing it could add.
        signal = {"remote_count": remote_n, "local_count": len(local),
                  "discrepancy_signalled": remote_n != len(local)}

    elif strategy == "partitioned_checksum":
        # Probe each partition with count + hash of sorted ids, then fetch
        # details only where the probe disagrees.
        for p in range(PARTITIONS):
            remote_ids = sorted(rid for rid in truth if partition_of(rid, PARTITIONS) == p)
            local_ids = sorted(rid for rid in local if partition_of(rid, PARTITIONS) == p)
            conn._call(vendor.count)          # the probe costs one call
            rh = hashlib.blake2b("|".join(remote_ids).encode()).hexdigest()
            lh = hashlib.blake2b("|".join(local_ids).encode()).hexdigest()
            if rh != lh:
                # The partition disagrees: pay to list its ids and diff.
                conn._call(vendor.list_ids)
                rs, ls = set(remote_ids), set(local_ids)
                found["missing"] |= (rs - ls)
                found["ghost"] |= (ls - rs)

    elif strategy == "full_id_inventory":
        offset = 0
        remote_ids = set()
        while True:
            page = conn._call(vendor.list_ids, offset=offset, limit=1000)
            if not page:
                break
            remote_ids |= set(page)
            offset += len(page)
            if len(page) < 1000:
                break
        found["missing"] |= (remote_ids - set(local))
        found["ghost"] |= (set(local) - remote_ids)

    elif strategy == "full_field_compare":
        ids = sorted(truth)
        for i in range(0, len(ids), vendor.batch_max):
            batch = ids[i:i + vendor.batch_max]
            rows = conn._call(vendor.get_many, batch)
            for r in rows:
                lr = local.get(r["id"])
                if lr is None:
                    found["missing"].add(r["id"])
                elif lr.get("version") != r.get("version"):
                    # The only strategy that sees this. A silently wrong VALUE
                    # is invisible to counts and to id inventories, because the
                    # identifier is present and correct on both sides.
                    found["wrong"].add(r["id"])
        found["ghost"] |= (set(local) - set(truth))

    else:
        raise ValueError("unknown strategy: %r" % (strategy,))

    return found, vendor.calls - calls_before, signal


def main():
    out = {"partitions": PARTITIONS, "interval_seconds": INTERVAL_SECONDS,
           "poll_config": {v: lab.good_poll_config(v)
                           for v in ("atlas", "beacon")}, "vendors": {}}

    for vendor_name in ("atlas", "beacon"):
        print("=== %s ===" % vendor_name)
        rows = []
        actual = None
        for strategy in ("none", "count_check", "partitioned_checksum",
                         "full_id_inventory", "full_field_compare"):
            # The poll is configured PER VENDOR, for the reason
            # lab.good_poll_config gives: Atlas needs the deletes endpoint,
            # Beacon needs the archived flag, and one shared literal cannot be
            # right for both.
            clock, vendor, lim, conn = lab.build_run(
                vendor_name,
                config=SyncConfig(**lab.good_poll_config(vendor_name)))
            conn.backfill()
            conn.run(until=W.TIMELINE_SECONDS, interval_seconds=INTERVAL_SECONDS)

            actual = drift_report(conn.store, vendor)
            found, cost, signal = reconcile(strategy, conn, vendor)

            detected = {k: len(found[k] & set(actual[k])) for k in actual}
            missed = {k: len(actual[k]) - detected[k] for k in actual}
            row = {
                "strategy": strategy,
                "calls": cost,
                "drift_present": {k: len(v) for k, v in actual.items()},
                "drift_detected": detected,
                "drift_missed": missed,
                "total_present": sum(len(v) for v in actual.values()),
                "total_detected": sum(detected.values()),
                "total_missed": sum(missed.values()),
                "signal": signal,
            }
            row["detection_rate"] = round(
                row["total_detected"] / row["total_present"], 6) \
                if row["total_present"] else 0.0
            rows.append(row)
            print("  %-22s calls %5d  detected %4d/%4d (%.4f)  "
                  "missing %3d ghost %3d wrong %3d"
                  % (strategy, cost, row["total_detected"],
                     row["total_present"], row["detection_rate"],
                     detected["missing"], detected["ghost"], detected["wrong"]))
        out["vendors"][vendor_name] = rows
        print()

    # ---- the verdict -------------------------------------------------------
    atlas = {r["strategy"]: r for r in out["vendors"]["atlas"]}
    count = atlas["count_check"]
    checksum = atlas["partitioned_checksum"]
    full = atlas["full_field_compare"]

    # COST PER 1,000 records, which is the figure that transfers. The absolute
    # call counts here are for a 4,000-record corpus and the ordering between
    # strategies depends on that size. A partitioned checksum pays a fixed
    # probe cost per partition, so at small corpus sizes it is strictly worse
    # than simply listing every id, which is exactly what this run shows. It
    # starts to win when a full inventory no longer fits in the call budget.
    for vname, rows_ in out["vendors"].items():
        for r in rows_:
            r["calls_per_1000_records"] = round(
                r["calls"] / (W.N_RECORDS / 1000.0), 2)
            r["detection_per_call"] = round(
                r["total_detected"] / r["calls"], 4) if r["calls"] else None

    print("count check detected %d of %d (%.1f%%) for %d call(s)"
          % (count["total_detected"], count["total_present"],
             100.0 * count["detection_rate"], count["calls"]))
    print("partitioned checksum detected %d of %d (%.1f%%) for %d calls"
          % (checksum["total_detected"], checksum["total_present"],
             100.0 * checksum["detection_rate"], checksum["calls"]))
    print("full field compare detected %d of %d (%.1f%%) for %d calls"
          % (full["total_detected"], full["total_present"],
             100.0 * full["detection_rate"], full["calls"]))
    print("ONLY full_field_compare sees a silently wrong VALUE: %d of them"
          % full["drift_detected"]["wrong"])

    inventory = atlas["full_id_inventory"]
    print()
    print("AT THIS CORPUS SIZE (%d records) THE COST ORDER IS NOT THE EXPECTED "
          "ONE:" % W.N_RECORDS)
    print("  full_id_inventory     %2d calls, %.1f%% detected"
          % (inventory["calls"], 100.0 * inventory["detection_rate"]))
    print("  partitioned_checksum  %2d calls, %.1f%% detected  "
          "-- strictly worse here" % (checksum["calls"],
                                      100.0 * checksum["detection_rate"]))
    print("  full_field_compare    %2d calls, %.1f%% detected  "
          "-- %.1fx the cheapest useful check, and it catches everything"
          % (full["calls"], 100.0 * full["detection_rate"],
             full["calls"] / max(inventory["calls"], 1)))
    print("A partitioned checksum pays a fixed probe cost per partition, so it")
    print("only wins once a full id inventory stops fitting in the budget.")

    held = count["detection_rate"] > 0.5
    out["prediction"] = dict(
        PREDICTION,
        count_check_detection_rate=count["detection_rate"],
        checksum_detection_rate=checksum["detection_rate"],
        full_compare_detection_rate=full["detection_rate"],
        checksum_calls=checksum["calls"],
        full_compare_calls=full["calls"],
        wrong_values_only_full_compare_sees=full["drift_detected"]["wrong"],
        full_compare_cost_multiple_of_id_inventory=round(
            full["calls"] / max(inventory["calls"], 1), 2),
        corpus_records=W.N_RECORDS,
        verdict="held" if held else "REFUTED")
    print("prediction: %s" % out["prediction"]["verdict"])

    lab.write_result("exp4_reconciliation", out)
    print("wrote results/exp4_reconciliation.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
