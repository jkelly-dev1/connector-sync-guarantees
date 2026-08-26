"""EXPERIMENT 3: do webhooks fix what incremental sync misses?

    python3 scripts/exp3_webhooks_vs_polling.py

Experiment 2 established that a modified-since watermark loses records. The
next thing every team reaches for is webhooks. This measures whether that
helps, what it costs, and what it does NOT fix.

FOUR CONFIGURATIONS over the same mutation timeline:
    poll_only            the experiment 2 baseline, correctly configured
    webhook_only         events are the only trigger
    webhook_plus_poll    the pattern the reference material recommends
    webhook_plus_poll_with_outage
                         the same, but the receiver is down for a window:
                         long enough that the vendor DISABLES the subscription

The event is a hint, not data. Each delivery causes a re-read of the named
record. Trusting the payload would layer an ordering bug on a delivery bug,
and a re-read is one call.

The metric worth publishing is the webhook-to-poll delta: how many changes the
poll found that the webhooks should have delivered. It is the only number that
tells an operator how much to trust their webhook path, and almost nobody
measures it.

The prediction, recorded before the run: webhooks plus polling detect
everything polling alone detects, and detect it sooner. Expect it HELD on
latency and REFUTED on completeness in the outage arm, where a silently
disabled subscription means the events stop and the receiver cannot tell.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lab
from sim import world as W
from sim.webhooks import WebhookChannel
from connector.sync import SyncConfig, score, score_by_mutation_kind

PREDICTION = {
    "claim": "webhooks plus polling detect every change polling alone "
             "detects, and detect it sooner",
}

INTERVAL_SECONDS = 300

# The receiver outage, in simulated seconds. Long enough to trip the vendor's
# consecutive-failure threshold and disable the subscription.
OUTAGE_FROM = 7200.0
OUTAGE_UNTIL = 10800.0

# The correctly configured poll, carried over from experiment 2.
GOOD_POLL = dict(use_tiebreaker=True, inclusive_bound=True,
                 overlap_seconds=120.0, use_deletes_api=True)


def run(vendor_name, use_webhooks, use_poll, outage=False):
    clock, vendor, lim, conn = lab.build_run(
        vendor_name, config=SyncConfig(**GOOD_POLL))
    conn.backfill()

    channel = WebhookChannel(vendor)
    deliveries = channel.deliveries_for(
        vendor.timeline,
        receiver_down_from=OUTAGE_FROM if outage else None,
        receiver_down_until=OUTAGE_UNTIL if outage else None)

    # Detection bookkeeping: for each mutation, the earliest simulated moment
    # the connector held the correct value for it.
    detected_at = {}
    seen_event_ids = set()
    duplicates_absorbed = 0
    stale_overwrites_prevented = 0
    webhook_detections = 0
    poll_detections = 0

    di = 0
    next_poll = clock.now()
    end = W.TIMELINE_SECONDS

    while clock.now() < end:
        # Whichever comes first: the next delivery or the next scheduled poll.
        next_delivery = deliveries[di][0] if (use_webhooks and di < len(deliveries)) else None
        candidates = [t for t in (next_delivery,
                                  next_poll if use_poll else None)
                      if t is not None]
        if not candidates:
            break
        t = min(candidates)
        if t > end:
            break
        clock.advance_to(t)
        vendor.apply_timeline_to_now()

        if next_delivery is not None and t == next_delivery:
            arrival, event = deliveries[di]
            di += 1
            if event["event_id"] in seen_event_ids:
                # At-least-once delivery, absorbed. De-duplicating on the event
                # id prevents double processing; the version check below is
                # what prevents an out-of-order overwrite, which is a different
                # problem with a different fix.
                duplicates_absorbed += 1
                continue
            seen_event_ids.add(event["event_id"])
            rows = conn._call(vendor.get_many, [event["record_id"]])
            for r in rows:
                prior = conn.store.records.get(r["id"])
                if prior is not None and prior.get("version", 0) > r.get("version", 0):
                    stale_overwrites_prevented += 1
                conn.store.upsert(r, source_version=r.get("version"))
            if not rows:
                conn.store.mark_deleted(event["record_id"])
            if event["seq"] not in detected_at:
                detected_at[event["seq"]] = clock.now()
                webhook_detections += 1
            continue

        # A scheduled poll.
        before = dict((k, v.get("version")) for k, v in conn.store.records.items())
        conn.incremental_pass()
        for m in vendor.timeline:
            if m["at"] > clock.now() or m["seq"] in detected_at:
                continue
            rid = m["record_id"]
            truth = vendor.records.get(rid)
            local = conn.store.records.get(rid)
            if m["kind"] in ("DELETE", "MERGE"):
                if rid in conn.store.deleted:
                    detected_at[m["seq"]] = clock.now()
                    poll_detections += 1
            elif truth is not None and local is not None \
                    and local.get("version") == truth.get("version"):
                detected_at[m["seq"]] = clock.now()
                poll_detections += 1
        next_poll = clock.now() + INTERVAL_SECONDS

    clock.advance_to(end)
    vendor.apply_timeline_to_now()

    happened = [m for m in vendor.timeline if m["at"] <= end]
    latencies = sorted(detected_at[m["seq"]] - m["at"]
                       for m in happened if m["seq"] in detected_at)
    missed = [m for m in happened if m["seq"] not in detected_at]

    def pct(vals, p):
        if not vals:
            return None
        k = max(0, min(len(vals) - 1,
                       int(round(p / 100.0 * len(vals) + 0.5)) - 1))
        return round(vals[k], 1)

    return {
        "use_webhooks": use_webhooks,
        "use_poll": use_poll,
        "outage": outage,
        "changes": len(happened),
        "detected": len(detected_at),
        "missed": len(missed),
        "miss_rate": round(len(missed) / len(happened), 6) if happened else 0.0,
        "detection_latency_p50_seconds": pct(latencies, 50),
        "detection_latency_p95_seconds": pct(latencies, 95),
        "detection_latency_max_seconds": pct(latencies, 100),
        "detected_by_webhook_first": webhook_detections,
        "detected_by_poll_first": poll_detections,
        "duplicates_absorbed": duplicates_absorbed,
        "stale_overwrites_prevented": stale_overwrites_prevented,
        "calls": vendor.calls,
        "channel": channel.stats(),
        "score": score(conn.store, vendor),
    }


def main():
    out = {"interval_seconds": INTERVAL_SECONDS,
           "outage_from": OUTAGE_FROM, "outage_until": OUTAGE_UNTIL,
           "poll_config": GOOD_POLL, "vendors": {}}

    for vendor_name in ("atlas", "beacon"):
        print("=== %s ===" % vendor_name)
        arms = [
            ("poll_only", dict(use_webhooks=False, use_poll=True)),
            ("webhook_only", dict(use_webhooks=True, use_poll=False)),
            ("webhook_plus_poll", dict(use_webhooks=True, use_poll=True)),
            ("webhook_plus_poll_outage",
             dict(use_webhooks=True, use_poll=True, outage=True)),
        ]
        rows = []
        for label, kwargs in arms:
            r = run(vendor_name, **kwargs)
            r["label"] = label
            rows.append(r)
            print("  %-26s missed %4d/%4d (%.4f)  p50 %6ss  max %7ss  calls %5d"
                  % (label, r["missed"], r["changes"], r["miss_rate"],
                     r["detection_latency_p50_seconds"],
                     r["detection_latency_max_seconds"], r["calls"]))
        out["vendors"][vendor_name] = rows

    # ---- the verdict -------------------------------------------------------
    atlas = {r["label"]: r for r in out["vendors"]["atlas"]}
    poll = atlas["poll_only"]
    both = atlas["webhook_plus_poll"]
    outage = atlas["webhook_plus_poll_outage"]

    # The webhook-to-poll delta: changes the poll caught that the webhook path
    # did not deliver first. This is the number that says how much the webhook
    # channel can be trusted.
    delta = both["detected_by_poll_first"]

    faster = (both["detection_latency_p50_seconds"]
              < poll["detection_latency_p50_seconds"])
    complete = both["missed"] <= poll["missed"]
    outage_complete = outage["missed"] <= poll["missed"]

    print()
    print("poll alone   p50 %ss, missed %d"
          % (poll["detection_latency_p50_seconds"], poll["missed"]))
    print("webhook+poll p50 %ss, missed %d"
          % (both["detection_latency_p50_seconds"], both["missed"]))
    print("webhook-to-poll delta: %d changes the poll caught first" % delta)
    # The number that says the channel never came back. Once the vendor
    # disables the subscription, every subsequent event is dropped at the
    # source: including after the receiver recovers. The receiver cannot
    # tell: a dead subscription and a quiet period look identical from the
    # inside, so subscription STATUS has to be monitored rather than receipt
    # rate.
    dropped_after = outage["channel"]["dropped_after_subscription_disabled"]
    print("outage arm   missed %d, subscription active at end: %s"
          % (outage["missed"], outage["channel"]["subscription_active_at_end"]))
    print("             %d events dropped at the source AFTER the "
          "subscription was disabled" % dropped_after)
    print("             it was disabled at t=%ss and never re-enabled"
          % outage["channel"]["disabled_at"])

    out["webhook_to_poll_delta"] = delta
    out["outage_events_dropped_after_disable"] = dropped_after
    out["prediction"] = dict(
        PREDICTION,
        faster_than_poll_alone=faster,
        as_complete_as_poll_alone=complete,
        outage_arm_as_complete=outage_complete,
        verdict="held" if (faster and complete and outage_complete)
                else "REFUTED")
    print("prediction: %s" % out["prediction"]["verdict"])

    lab.write_result("exp3_webhooks_vs_polling", out)
    print("wrote results/exp3_webhooks_vs_polling.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
