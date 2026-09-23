"""Invariants over the shipped results/*.json.

These are not regression tests against frozen numbers in the usual sense; they
can be, because the simulated clock makes every figure exactly reproducible.
But a test that pinned every number would fail for any deliberate change to the
world and teach nothing. Each test below states a PROPERTY that has to hold of
any correct run.
"""

import pytest


# ---------------------------------------------------------------------------
# EXPERIMENT 1: RATE LIMITS
# ---------------------------------------------------------------------------

def test_pacing_helps_under_a_rate_ceiling(exp1):
    runs = exp1["scenarios"]["rate_bound"]["runs"]
    paced = [r for r in runs if r["strategy"] != "naive"]
    assert all(r["completed"] for r in paced)
    # Every paced strategy does the job in the minimum number of calls; the
    # unpaced one does not.
    assert len({r["calls"] for r in paced}) == 1


def test_the_naive_limiter_wastes_calls_it_did_not_need(exp1):
    # (mutation-checked: make a rejected call free of both time and quota and
    # this collapses: naive then looks strictly better than every paced
    # strategy)
    runs = {r["strategy"]: r for r in exp1["scenarios"]["rate_bound"]["runs"]}
    naive = runs["naive"]
    paced = runs["token_bucket"]
    assert naive["calls"] > paced["calls"]
    assert naive["calls_throttled"] > 0
    assert paced["calls_throttled"] == 0


def test_no_limiter_strategy_survives_a_daily_cap(exp1):
    # The point of separating the two scenarios. A per-second limit is a
    # throughput problem that pacing solves; a daily cap is a capacity problem
    # that pacing cannot touch.
    runs = exp1["scenarios"]["cap_bound"]["runs"]
    assert runs, "the cap-bound scenario must have run"
    assert not any(r["completed"] for r in runs)


def test_the_access_pattern_dominates_the_limiter_choice(exp1):
    # Tuning a limiter moves a small multiple. Changing the endpoint moves an
    # order of magnitude.
    ap = {r["pattern"]: r for r in exp1["access_patterns"]["rate_bound"]}
    scan = ap["paged_scan"]
    n1 = ap["paged_scan_plus_detail_fetch"]
    bulk = ap["bulk_export"]
    assert n1["calls"] > scan["calls"] * 50
    assert bulk["calls"] < scan["calls"]
    assert exp1["prediction"]["n_plus_one_call_multiple"] > 50


def test_the_bulk_path_trades_calls_for_latency(exp1):
    # It is not free: one call, and a long wait. That is why it belongs to
    # backfill and not to a five-minute incremental sync.
    ap = {r["pattern"]: r for r in exp1["access_patterns"]["rate_bound"]}
    assert ap["bulk_export"]["simulated_seconds"] > \
        ap["paged_scan"]["simulated_seconds"]


def test_every_access_pattern_can_report_that_it_did_not_finish(exp1):
    # The SHIPPED half of the property. Both cap_bound scenarios in the
    # published results are ones where the paged scan genuinely finishes, so
    # this cannot distinguish an honest row from a structurally unfalsifiable
    # one on its own; the test below it does that, by running the experiment
    # against an allowance the paged scan cannot survive.
    cap = exp1["access_patterns"]["cap_bound"]
    assert cap, "the cap-bound access patterns must have run"
    by_pattern = {r["pattern"]: r for r in cap}
    assert by_pattern["paged_scan_plus_detail_fetch"]["completed"] is False
    # The cheap pattern under the same cap does finish, so "completed" is
    # not being satisfied by reporting no everywhere.
    assert by_pattern["paged_scan"]["completed"] is True


def test_a_paged_scan_that_ran_out_of_quota_reports_that_it_did_not_finish():
    # (mutation-checked: drop `and not conn.quota_exhausted` from the
    # paged_scan arm and it reports completed=True holding 2,000 of 4,000
    # records)
    #
    # Why this runs the experiment instead of reading its results:
    # Connector.backfill swallows QuotaExhausted (it breaks out of the page
    # loop and returns), so an arm that decides "completed" from whether
    # backfill() raised can only ever answer yes. In BOTH published scenarios
    # the paged scan really does finish (21 calls against an allowance of 100),
    # so the shipped results are identical either way and no assertion over
    # them can see the difference. The allowance below is one the paged scan
    # cannot survive, which is the only place the two answers part company.
    import exp1_rate_limits

    rows = exp1_rate_limits.access_patterns(
        {"daily_allowance": 10, "per_second_limit": 5.0, "page_size": 200})
    scan = {r["pattern"]: r for r in rows}["paged_scan"]
    assert scan["completed"] is False
    assert 0 < scan["records"] < 4000


def test_the_n_plus_one_pattern_cannot_fit_under_the_cap(exp1):
    ap = {r["pattern"]: r for r in exp1["access_patterns"]["cap_bound"]}
    assert ap["paged_scan"]["completed"] is True
    assert ap["paged_scan_plus_detail_fetch"]["completed"] is False


def test_the_prediction_about_limiters_is_qualified_not_held(exp1):
    assert exp1["prediction"]["verdict"] == "QUALIFIED"


# ---------------------------------------------------------------------------
# EXPERIMENT 2: INCREMENTAL LOSS
# ---------------------------------------------------------------------------

def test_incremental_sync_does_not_catch_everything(exp2):
    # The headline. Scored on the BEST configuration, not the naive one;
    # refuting it with the naive setup would prove nothing.
    assert exp2["prediction"]["verdict"] == "REFUTED"
    assert exp2["prediction"]["best_config_wrong_records"] > 0


def test_a_silent_update_is_missed_at_every_setting(exp2):
    # No overlap, no tiebreaker and no deletes endpoint can recover a change
    # that never moved the timestamp being filtered on.
    for vendor, data in exp2["vendors"].items():
        for row in data["ladder"]:
            silent = row["by_mutation_kind"].get("SILENT_UPDATE")
            if silent and silent["happened"]:
                assert silent["missed"] > 0, (vendor, row["label"])


def test_only_a_deletes_mechanism_removes_ghost_records(exp2):
    # Ghost records are records that no longer exist upstream. A watermark scan
    # cannot see an absence, so no amount of overlap touches them.
    ladder = {r["label"]: r for r in exp2["vendors"]["atlas"]["ladder"]}
    before = ladder["+overlap_300s"]["score"]["ghost_records"]
    after = ladder["+deletes_api"]["score"]["ghost_records"]
    assert before > after
    assert after < before / 10


def test_archive_semantics_detect_deletion_without_a_special_endpoint(exp2):
    # The single difference between the two vendors, and it is decisive.
    ladder = {r["label"]: r for r in exp2["vendors"]["beacon"]["ladder"]}
    assert "+scan_archived" in ladder
    assert ladder["+scan_archived"]["score"]["ghost_records"] < \
        ladder["+overlap_300s"]["score"]["ghost_records"] / 10


def test_a_wider_overlap_never_makes_accuracy_worse(exp2):
    for vendor, data in exp2["vendors"].items():
        rows = sorted(data["overlap_sweep"], key=lambda r: r["overlap_seconds"])
        wrong = [r["score"]["wrong"] for r in rows]
        assert wrong == sorted(wrong, reverse=True), vendor


def test_a_wider_overlap_costs_redundant_reads(exp2):
    for vendor, data in exp2["vendors"].items():
        rows = sorted(data["overlap_sweep"], key=lambda r: r["overlap_seconds"])
        reads = [r["redundant_upserts"] for r in rows]
        assert reads[-1] > reads[0]


def test_the_overlap_window_has_sharp_diminishing_returns(exp2):
    # A finding that decides a setting. Each later increment must cost far
    # more redundant reads per record recovered than the first one did.
    rows = sorted(exp2["vendors"]["atlas"]["overlap_sweep"],
                  key=lambda r: r["overlap_seconds"])
    first, last = rows[0], rows[-1]
    recovered = first["score"]["wrong"] - last["score"]["wrong"]
    extra_reads = last["redundant_upserts"] - first["redundant_upserts"]
    assert recovered >= 0
    assert extra_reads > recovered * 100


def test_nothing_is_ever_missing_only_stale_or_ghosted(exp2):
    # A backfill that retrieved everything means no record is absent; what goes
    # wrong afterwards is staleness and undetected deletion, which is a more
    # precise statement of the problem than "the data is bad".
    for vendor, data in exp2["vendors"].items():
        for row in data["ladder"]:
            assert row["score"]["missing"] == 0, (vendor, row["label"])


# ---------------------------------------------------------------------------
# EXPERIMENT 3: WEBHOOKS
# ---------------------------------------------------------------------------

def test_webhooks_detect_changes_far_sooner_than_polling(exp3):
    for vendor, rows in exp3["vendors"].items():
        arms = {r["label"]: r for r in rows}
        assert arms["webhook_plus_poll"]["detection_latency_p50_seconds"] < \
            arms["poll_only"]["detection_latency_p50_seconds"] / 5


def test_webhooks_alone_still_miss_changes(exp3):
    # Best-effort delivery is not a correctness mechanism.
    for vendor, rows in exp3["vendors"].items():
        arms = {r["label"]: r for r in rows}
        assert arms["webhook_only"]["missed"] > 0


def test_webhooks_plus_polling_beats_either_alone(exp3):
    for vendor, rows in exp3["vendors"].items():
        arms = {r["label"]: r for r in rows}
        both = arms["webhook_plus_poll"]["missed"]
        assert both < arms["poll_only"]["missed"]
        assert both < arms["webhook_only"]["missed"]


def test_the_latency_is_bought_with_calls(exp3):
    # It is not free: every delivery causes a re-read.
    for vendor, rows in exp3["vendors"].items():
        arms = {r["label"]: r for r in rows}
        assert arms["webhook_plus_poll"]["calls"] > arms["poll_only"]["calls"]


def test_a_receiver_outage_permanently_disables_the_subscription(exp3):
    # The result worth leading with. The vendor stops sending, the receiver
    # cannot tell, and it never comes back on its own.
    arms = {r["label"]: r for r in exp3["vendors"]["atlas"]}
    outage = arms["webhook_plus_poll_outage"]
    assert outage["channel"]["subscription_active_at_end"] is False
    assert outage["channel"]["disabled_at"] is not None
    assert exp3["outage_events_dropped_after_disable"] > 0
    assert outage["missed"] > arms["webhook_plus_poll"]["missed"] * 5


def test_the_webhook_to_poll_delta_is_published(exp3):
    # One metric says how much the webhook path can be trusted, and the one
    # almost nobody measures.
    assert exp3["webhook_to_poll_delta"] > 0


def test_duplicate_deliveries_are_absorbed_rather_than_double_processed(exp3):
    for vendor, rows in exp3["vendors"].items():
        arms = {r["label"]: r for r in rows}
        assert arms["webhook_plus_poll"]["duplicates_absorbed"] > 0


def test_no_arm_credits_a_deletion_its_own_store_does_not_reflect(exp3):
    # (mutation-checked with both halves together: Vendor.get_many handing the
    # tombstone back and the webhook branch crediting a delivery on arrival
    # instead of on reflection. Atlas webhook_only then reports 97 credited
    # against 0 reflected. Neither half alone moves this world's numbers, so
    # the reflection guard is defense in depth here rather than the
    # load-bearing half.)
    #
    # The asymmetry this forbids. A poll credits a deletion only when the
    # local copy agrees the record is gone. A webhook arm could credit one
    # simply because a delivery arrived and was processed, which would report
    # a detection rate the store does not support.
    #
    # Detection means the connector's own copy is right, not that something
    # told it to look. Nothing downstream of that distinction can be trusted
    # if the two numbers disagree.
    for vendor, rows in exp3["vendors"].items():
        for r in rows:
            assert r["deletions_happened"] > 0, (vendor, r["label"])
            assert (r["deletions_credited_as_detected"]
                    == r["deletions_reflected_at_end"]), (vendor, r["label"])


def test_a_webhook_only_arm_learns_its_deletions_from_a_not_found(exp3):
    # (mutation-checked: let Vendor.get_many return the tombstone and both
    # webhook_only arms drop to 0 reflected and 104 / 124 ghosts)
    #
    # A webhook-only arm never polls, so it never reaches the deletes endpoint
    # and never scans for the archived flag. The ONLY way it can learn that a
    # record is gone is the re-read coming back empty. If that arm reflects no
    # deletions at all, absence is not being read as a signal, and every
    # deleted record is a ghost in its local copy however good its latency
    # figures look.
    for vendor, rows in exp3["vendors"].items():
        arm = {r["label"]: r for r in rows}["webhook_only"]
        assert arm["deletions_reflected_at_end"] > 0, vendor
        assert arm["score"]["ghost_records"] < arm["deletions_happened"], vendor


@pytest.mark.parametrize("name", ["exp3", "exp4"])
def test_each_vendor_is_polled_with_its_own_delete_mechanism(name, request):
    # (mutation-checked: hand both vendors one shared poll_config with
    # use_deletes_api=True and beacon's entry fails)
    #
    # Atlas hard-deletes and has a dedicated deletes endpoint. Beacon archives
    # and has NO such endpoint: get_deleted raises NotImplementedError, the
    # connector swallows it, and the arm then detects none of its deletions
    # while still being described as correctly configured. Experiment 2 makes
    # this substitution at its last ladder rung; the experiments downstream of
    # it have to make the same one.
    cfg = request.getfixturevalue(name)["poll_config"]
    assert set(cfg) == {"atlas", "beacon"}
    assert cfg["atlas"]["use_deletes_api"] is True
    assert cfg["atlas"].get("scan_archived", False) is False
    assert cfg["beacon"]["scan_archived"] is True
    assert cfg["beacon"].get("use_deletes_api", False) is False


# ---------------------------------------------------------------------------
# EXPERIMENT 4: RECONCILIATION
# ---------------------------------------------------------------------------

def test_a_count_check_catches_essentially_nothing(exp4):
    # Refutes the prediction. A count names no record, and equal-and-opposite
    # errors cancel out entirely.
    assert exp4["prediction"]["verdict"] == "REFUTED"
    rows = {r["strategy"]: r for r in exp4["vendors"]["atlas"]}
    assert rows["count_check"]["detection_rate"] == 0.0
    assert rows["count_check"]["calls"] <= 2


def test_a_count_check_signals_a_discrepancy_and_still_names_no_record(exp4):
    # (mutation-checked: make the count check add the drift it cannot see to
    # `found`, and this fails on total_detected; make local and remote counts
    # equal, and it fails on discrepancy_signalled)
    #
    # The zero is the finding, and it has to be a measurement: a detection
    # rate of 0.0000 from a line that cannot produce anything else could not
    # fail on a broken or deleted strategy either. What distinguishes "detects
    # nothing" from "did not run" is that the check fired: the counts really are unequal here, by
    # exactly the one ghost the best incremental configuration leaves behind.
    for vendor, rows in exp4["vendors"].items():
        r = {x["strategy"]: x for x in rows}["count_check"]
        signal = r["signal"]
        assert signal["discrepancy_signalled"] is True, vendor
        assert signal["remote_count"] != signal["local_count"], vendor
        assert r["calls"] == 1, vendor
        # It fired, it cost a call, and it named nothing.
        assert r["total_detected"] == 0, vendor
        assert r["total_present"] > 0, vendor


def test_only_a_field_comparison_sees_a_silently_wrong_value(exp4):
    # An identifier-level check cannot see it: the id is present and correct on
    # both sides, and only the value differs.
    for vendor, rows_ in exp4["vendors"].items():
        rows = {r["strategy"]: r for r in rows_}
        for name in ("count_check", "partitioned_checksum",
                     "full_id_inventory"):
            assert rows[name]["drift_detected"]["wrong"] == 0, (vendor, name)
        assert rows["full_field_compare"]["drift_detected"]["wrong"] > 0


def test_a_full_field_comparison_detects_everything(exp4):
    for vendor, rows_ in exp4["vendors"].items():
        rows = {r["strategy"]: r for r in rows_}
        assert rows["full_field_compare"]["detection_rate"] == 1.0


def test_the_do_nothing_baseline_detects_nothing_and_costs_nothing(exp4):
    rows = {r["strategy"]: r for r in exp4["vendors"]["atlas"]}
    assert rows["none"]["calls"] == 0
    assert rows["none"]["total_detected"] == 0
    assert rows["none"]["total_present"] > 0


def test_the_cost_ordering_is_reported_per_thousand_records(exp4):
    # The absolute call counts do not transfer; the per-record cost does, and
    # the README says which ordering depends on corpus size.
    for vendor, rows_ in exp4["vendors"].items():
        for r in rows_:
            assert "calls_per_1000_records" in r


def test_drift_is_dominated_by_wrong_values_not_missing_records(exp4):
    # This is why the cheap identifier-level checks fail here: the damage
    # incremental sync leaves is mostly values, not membership.
    rows = {r["strategy"]: r for r in exp4["vendors"]["atlas"]}
    present = rows["full_field_compare"]["drift_present"]
    assert present["wrong"] > present["missing"]


# ---------------------------------------------------------------------------
# Every result names the input it was measured on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["exp1", "exp2", "exp3", "exp4"])
def test_every_result_carries_the_manifest_of_its_input(name, request):
    data = request.getfixturevalue(name)
    assert len(data["input_manifest_sha256"]) == 64


def test_all_four_experiments_measured_the_same_generated_world(
        exp1, exp2, exp3, exp4):
    shas = {d["input_manifest_sha256"] for d in (exp1, exp2, exp3, exp4)}
    assert len(shas) == 1
