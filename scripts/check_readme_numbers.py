"""Re-derive every published figure from results/*.json and require it verbatim
in README.md.

    python3 scripts/check_readme_numbers.py

Why this exists. A number in a document has no owner. The results files are
rewritten by every run; the prose is rewritten by hand, sometimes, when
somebody remembers. This makes the prose fail instead of drift.

It covers sentences, not only table rows. A figure inside a paragraph does not
look like a figure to a reader or to whoever writes a deriver, so it is the one
most likely to go stale.

It prints how many figures it checked whether or not any are missing, so a
version that has quietly stopped deriving half of them is visible rather than
clean.

Unlike the sibling timing repository, this one never goes red on a re-run.
Every figure is derived from a simulated clock and a seeded world, so re-running
the experiments reproduces the results byte for byte and the README stays
correct. A failure here means the CODE changed, which is exactly when a human
should look.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import charts
import lab

README = os.path.join(lab.REPO, "README.md")


def n(x):
    return "{:,}".format(int(x))


def f(x, places):
    return "%.*f" % (places, float(x))


def fc(x, places):
    """A float WITH thousands separators, which is how the README writes any
    figure large enough to need them. Deriving them without would make the
    checker demand a less readable document than the one a human would write."""
    return "{:,.{p}f}".format(float(x), p=places)


def build():
    e1 = lab.read_result("exp1_rate_limits")
    e2 = lab.read_result("exp2_incremental_loss")
    e3 = lab.read_result("exp3_webhooks_vs_polling")
    e4 = lab.read_result("exp4_reconciliation")
    want = []

    def add(label, s):
        want.append((label, s))

    # ---- experiment 1 ------------------------------------------------------
    for r in e1["scenarios"]["rate_bound"]["runs"]:
        add("e1 rate row %s" % r["strategy"],
            "| %s | %s | %s | %s | %s |"
            % (r["strategy"], "yes" if r["completed"] else "**no**",
               n(r["calls"]), n(r["calls_throttled"]),
               f(r["simulated_seconds"], 2)))
    for r in e1["scenarios"]["cap_bound"]["runs"]:
        add("e1 cap row %s" % r["strategy"],
            "| %s | %s | %s | %s | %s |"
            % (r["strategy"], "yes" if r["completed"] else "**no**",
               n(r["calls"]), n(r["calls_throttled"]), n(r["records_held"])))
    label_of = {"paged_scan": "paged scan",
                "paged_scan_plus_detail_fetch":
                    "paged scan + a detail fetch per record",
                "bulk_export": "async bulk export"}
    for r in e1["access_patterns"]["rate_bound"]:
        add("e1 access row %s" % r["pattern"], "| %s | %s | %s | %s |"
            % (label_of[r["pattern"]], "yes" if r["completed"] else "no",
               n(r["calls"]), fc(r["simulated_seconds"], 2)))
    naive = [r for r in e1["scenarios"]["rate_bound"]["runs"]
             if r["strategy"] == "naive"][0]
    paced = [r for r in e1["scenarios"]["rate_bound"]["runs"]
             if r["strategy"] == "token_bucket"][0]
    add("e1 naive prose", "finishes in %s simulated seconds by issuing %s\ncalls to do %s calls of\nwork: %s of them rejected"
        % (f(naive["simulated_seconds"], 2), n(naive["calls"]),
           n(paced["calls"]), n(naive["calls_throttled"])))
    add("e1 minimum calls", "minimum %s calls" % n(paced["calls"]))
    add("e1 spread", "spread between them is %sx"
        % f(e1["prediction"]["rate_bound_spread"], 2))
    add("e1 n+1 multiple", "costs %sx the calls of a paged scan"
        % f(e1["prediction"]["n_plus_one_call_multiple"], 1))
    add("e1 spread repeated", "limiter moved %sx"
        % f(e1["prediction"]["rate_bound_spread"], 2))
    cap_naive = [r for r in e1["scenarios"]["cap_bound"]["runs"]
                 if r["strategy"] == "naive"][0]
    cap_tb = [r for r in e1["scenarios"]["cap_bound"]["runs"]
              if r["strategy"] == "token_bucket"][0]
    cap_aimd = [r for r in e1["scenarios"]["cap_bound"]["runs"]
                if r["strategy"] == "aimd"][0]
    # Name the strategies rather than say "the others". Deriving the sentence
    # from token_bucket alone made a claim about all three, and aimd does not
    # match it; the table two lines above the sentence shows 1,860.
    add("e1 cap prose",
        "naive got %s records where the two paced limiters got %s\nand aimd got %s"
        % (n(cap_naive["records_held"]), n(cap_tb["records_held"]),
           n(cap_aimd["records_held"])))
    bulk = [r for r in e1["access_patterns"]["rate_bound"]
            if r["pattern"] == "bulk_export"][0]
    add("e1 bulk latency", "one call, and %s simulated seconds of job"
        % n(bulk["simulated_seconds"]))

    # ---- experiment 2 ------------------------------------------------------
    ladder_label = {"naive": "naive", "+tiebreaker": "+ tiebreaker",
                    "+inclusive_bound": "+ inclusive bound",
                    "+overlap_60s": "+ overlap 60s",
                    "+overlap_300s": "+ overlap 300s",
                    "+deletes_api": "+ deletes endpoint"}
    for r in e2["vendors"]["atlas"]["ladder"]:
        s = r["score"]
        last = r["label"] == "+deletes_api"
        ghost = ("**%s**" % n(s["ghost_records"])) if last else n(s["ghost_records"])
        wrong = ("**%s**" % n(s["wrong"])) if last else n(s["wrong"])
        add("e2 ladder %s" % r["label"], "| %s | %s | %s | %s | %s |"
            % (ladder_label[r["label"]], f(s["accuracy"], 4), n(s["stale"]),
               ghost, wrong))
    for r in e2["vendors"]["atlas"]["overlap_sweep"]:
        add("e2 overlap %s" % r["overlap_seconds"], "| %ds | %s | %s |"
            % (int(r["overlap_seconds"]), n(r["score"]["wrong"]),
               n(r["redundant_upserts"])))
    blame = e2["prediction"]["wrong_records_by_cause"]
    total_wrong = blame["wrong_records"]
    for kind, cnt in blame["attributed_to_kind"].items():
        add("e2 blame %s" % kind, "| `%s` | %s of %s |"
            % (kind, n(cnt), n(total_wrong)))
    silent_n = blame["attributed_to_kind"].get("SILENT_UPDATE", 0)
    add("e2 silent share", "are %d percent of the remaining damage"
        % round(100.0 * silent_n / total_wrong))
    add("e2 silent miss rate", "missed\n%s percent of the time"
        % f(100.0 * e2["prediction"]["silent_update_miss_rate"], 1))
    ov = {r["overlap_seconds"]: r for r in e2["vendors"]["atlas"]["overlap_sweep"]}
    recovered = ov[120.0]["score"]["wrong"] - ov[1800.0]["score"]["wrong"]
    extra = ov[1800.0]["redundant_upserts"] - ov[120.0]["redundant_upserts"]
    add("e2 overlap knee", "recovers %s\nrecord and costs %s extra redundant reads"
        % ({1: "one"}.get(recovered, n(recovered)), n(extra)))
    bl = {r["label"]: r for r in e2["vendors"]["beacon"]["ladder"]}
    add("e2 beacon overlap row", "| + overlap 300s | %s | %s |"
        % (f(bl["+overlap_300s"]["score"]["accuracy"], 4),
           n(bl["+overlap_300s"]["score"]["ghost_records"])))
    add("e2 beacon archived row", "| + scan for the archived flag | %s | **%s** |"
        % (f(bl["+scan_archived"]["score"]["accuracy"], 4),
           n(bl["+scan_archived"]["score"]["ghost_records"])))
    la = {r["label"]: r for r in e2["vendors"]["atlas"]["ladder"]}
    add("e2 ghost headline", "ghost records: %s to %s"
        % (n(la["+overlap_300s"]["score"]["ghost_records"]),
           n(la["+deletes_api"]["score"]["ghost_records"])))

    # ---- experiment 3 ------------------------------------------------------
    arm_label = {"poll_only": "poll only", "webhook_only": "webhook only",
                 "webhook_plus_poll": "webhook + poll",
                 "webhook_plus_poll_outage":
                     "webhook + poll, receiver outage"}
    arms = {r["label"]: r for r in e3["vendors"]["atlas"]}
    for label, r in ((l, arms[l]) for l in
                     ("poll_only", "webhook_only", "webhook_plus_poll",
                      "webhook_plus_poll_outage")):
        missed = "%s / %s" % (n(r["missed"]), n(r["changes"]))
        if label == "webhook_plus_poll":
            missed = "**%s**" % missed
        add("e3 row %s" % label, "| %s | %s | %ss | %ss | %ss | %s |"
            % (arm_label[label], missed,
               fc(r["detection_latency_p50_seconds"], 1),
               fc(r["detection_latency_p95_seconds"], 1),
               fc(r["detection_latency_max_seconds"], 1), n(r["calls"])))
    poll, both = arms["poll_only"], arms["webhook_plus_poll"]
    out_arm = arms["webhook_plus_poll_outage"]
    add("e3 speedup", "is %dx faster to detect and misses %dx less"
        % (round(poll["detection_latency_p50_seconds"]
                 / both["detection_latency_p50_seconds"]),
           round(poll["missed"] / both["missed"])))
    add("e3 call multiple", "costs %sx the calls"
        % f(both["calls"] / poll["calls"], 1))
    add("e3 webhook only missed", "still miss %s changes" % n(arms["webhook_only"]["missed"]))
    add("e3 duplicates", "%s duplicate deliveries were absorbed"
        % n(both["duplicates_absorbed"]))
    add("e3 outage window", "down from t=%s to t=%s"
        % (n(e3["outage_from"]), n(e3["outage_until"])))
    add("e3 disabled at", "disabled the subscription at\nt=%s seconds"
        % fc(out_arm["channel"]["disabled_at"], 1))
    add("e3 dropped after", "%s further events were dropped at\nthe source"
        % n(e3["outage_events_dropped_after_disable"]))
    add("e3 delta", "webhook-to-poll delta is %s" % n(e3["webhook_to_poll_delta"]))

    # ---- experiment 4 ------------------------------------------------------
    strat_label = {"none": "none", "count_check": "count check",
                   "partitioned_checksum": "partitioned checksum",
                   "full_id_inventory": "full id inventory",
                   "full_field_compare": "full field compare"}
    rows = {r["strategy"]: r for r in e4["vendors"]["atlas"]}
    for name in ("none", "count_check", "partitioned_checksum",
                 "full_id_inventory", "full_field_compare"):
        r = rows[name]
        det = "%s / %s" % (n(r["total_detected"]), n(r["total_present"]))
        rate = f(r["detection_rate"], 4)
        if name == "full_field_compare":
            det, rate = "**%s**" % det, "**%s**" % rate
        add("e4 row %s" % name, "| %s | %s | %s | %s | %s |"
            % (strat_label[name], n(r["calls"]),
               f(r["calls_per_1000_records"], 2), det, rate))
    present = rows["full_field_compare"]["drift_present"]
    add("e4 drift shape", "%s wrong records, of which %s are wrong VALUES and %s is a\nghost"
        % (n(rows["full_field_compare"]["total_present"]),
           n(present["wrong"]), n(present["ghost"])))
    add("e4 count rate", "is refuted at %s" % f(rows["count_check"]["detection_rate"], 4))
    add("e4 wrong count", "that is %s of the %s problems"
        % (n(present["wrong"]), n(rows["full_field_compare"]["total_present"])))
    # Re-keyed 2026-08-29: the expected literal carried the bold markers the
    # sentence in README.md used to wrap in, and the markers are gone from the
    # prose, so the literal quotes what ships.
    add("e4 cost multiple", "%s calls, five times the\ncheapest useful check"
        % n(rows["full_field_compare"]["calls"]))
    add("e4 checksum cost", "%s calls and is strictly worse than simply listing every id"
        % n(rows["partitioned_checksum"]["calls"]))

    # ---- the world ---------------------------------------------------------
    add("world size", "%s records per vendor" % n(4000))

    return want


def build_charts():
    """The rendered ASCII charts, which are checked EXACTLY rather than with
    whitespace flattened.

    Why they get their own path. Every other figure is compared after
    collapsing runs of whitespace, because prose legitimately re-wraps and a
    checker that failed on a line break would be turned off within a week. A
    bar chart is the opposite case: the whitespace IS the data. Flattened, a
    chart whose bars had all become the same length would still pass, which
    would leave the one figure a reader believes without checking as the one
    figure nothing checks.
    """
    return [(title, "```\n" + fn() + "\n```") for title, fn in charts.CHARTS]


def main():
    with open(README, encoding="utf-8") as fh:
        readme = fh.read()
    flat = re.sub(r"\s+", " ", readme)
    want = build()
    missing = [(l, s) for l, s in want
               if re.sub(r"\s+", " ", s) not in flat]

    # The CHARTS, matched character for character. See build_charts().
    want_charts = build_charts()
    missing += [("chart: " + l, s) for l, s in want_charts if s not in readme]

    print("%d figures and %d charts re-derived from results/*.json and "
          "checked against README.md" % (len(want), len(want_charts)))
    if missing:
        print()
        for l, s in missing:
            print("MISSING (%s):" % l)
            print("    %s" % s)
        print()
        print("%d of %d figures and charts are not in README.md verbatim."
              % (len(missing), len(want) + len(want_charts)))
        return 1
    print("all present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
