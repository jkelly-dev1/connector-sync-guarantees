"""The connector: the local store, the watermark logic, and the scoring.

These are the rules that decide what the connector ends up believing, so each
one is tested as a pure function rather than inferred from a run.
"""

import pytest

from connector.sync import Connector, Store, SyncConfig, score, \
    score_by_mutation_kind
from sim.clock import Clock
from sim import limiters
from sim.vendors import Atlas


# ---------------------------------------------------------------------------
# THE STORE
# ---------------------------------------------------------------------------

def _rec(rid="A-000001", version=1, **kw):
    r = {"id": rid, "version": version, "last_modified": 0.0,
         "deleted": False, "archived": False, "stage": "new"}
    r.update(kw)
    return r


def test_upsert_is_idempotent_on_the_source_id():
    # Retries, replayed webhooks and the backfill overlap all deliver the same
    # record twice. An idempotent upsert makes every one of those a non-event.
    s = Store()
    s.upsert(_rec(), source_version=1)
    s.upsert(_rec(), source_version=1)
    assert len(s.records) == 1
    assert s.upserts == 2


def test_a_repeat_of_the_same_version_is_counted_as_redundant():
    # This counter is how the cost of an overlap window becomes visible.
    s = Store()
    s.upsert(_rec(version=3), source_version=3)
    s.upsert(_rec(version=3), source_version=3)
    assert s.redundant_upserts == 1


def test_an_out_of_order_delivery_cannot_revert_a_newer_record():
    # (mutation-checked: remove the version check and event 1 arriving after
    # event 2 silently undoes event 2. That bug is very hard to reproduce and
    # is usually found by a confused end user)
    s = Store()
    s.upsert(_rec(version=5, stage="closed"), source_version=5)
    s.upsert(_rec(version=2, stage="new"), source_version=2)
    assert s.records["A-000001"]["stage"] == "closed"


def test_a_newer_delivery_does_apply():
    s = Store()
    s.upsert(_rec(version=2, stage="new"), source_version=2)
    s.upsert(_rec(version=6, stage="closed"), source_version=6)
    assert s.records["A-000001"]["stage"] == "closed"


def test_marking_deleted_removes_a_record_from_live_but_keeps_it():
    # Soft delete locally. Downstream consumers may hold references, and a hard
    # delete creates dangling ones.
    s = Store()
    s.upsert(_rec(), source_version=1)
    s.mark_deleted("A-000001")
    assert "A-000001" not in s.live()
    assert "A-000001" in s.records


def test_marking_the_same_record_deleted_twice_counts_once():
    s = Store()
    s.upsert(_rec(), source_version=1)
    s.mark_deleted("A-000001")
    s.mark_deleted("A-000001")
    assert s.delete_marks == 1


def test_an_upsert_after_a_delete_resurrects_the_record():
    # A record can be deleted and re-created upstream, and the local copy must
    # follow it back to life.
    s = Store()
    s.upsert(_rec(), source_version=1)
    s.mark_deleted("A-000001")
    s.upsert(_rec(version=2), source_version=2)
    assert "A-000001" in s.live()


# ---------------------------------------------------------------------------
# THE SYNC CONFIGURATION
# ---------------------------------------------------------------------------

def test_the_defaults_are_the_naive_ones():
    # DELIBERATE. The defaults are what "we do incremental sync on
    # last_modified" means before anybody has been paged, and experiment 2
    # scores that configuration first.
    c = SyncConfig()
    assert c.overlap_seconds == 0.0
    assert c.inclusive_bound is False
    assert c.use_tiebreaker is False
    assert c.use_deletes_api is False


def test_every_knob_round_trips_through_as_dict():
    c = SyncConfig(overlap_seconds=60, inclusive_bound=True,
                   use_tiebreaker=True, use_deletes_api=True,
                   scan_archived=True, page_size=50)
    d = c.as_dict()
    assert d["overlap_seconds"] == 60.0
    assert all(d[k] is True for k in ("inclusive_bound", "use_tiebreaker",
                                      "use_deletes_api", "scan_archived"))
    assert d["page_size"] == 50


# ---------------------------------------------------------------------------
# Backfill and the watermark HANDOFF
# ---------------------------------------------------------------------------

def _conn(config=None, timeline=None, **vendor_kw):
    clock = Clock()
    vendor = Atlas(clock, timeline=timeline)
    for k, v in vendor_kw.items():
        setattr(vendor, k, v)
    lim = limiters.build("token_bucket", clock, vendor.per_second_limit, 0.5)
    return clock, vendor, Connector(vendor, clock, lim, Store(),
                                    config or SyncConfig(page_size=200))


def test_backfill_retrieves_every_record():
    clock, vendor, conn = _conn()
    conn.backfill()
    assert len(conn.store.records) == len(vendor.records)


def test_the_watermark_is_set_from_the_START_of_the_backfill():
    # The HANDOFF bug, asserted. A backfill that runs for hours and then sets
    # the watermark to its FINISH time permanently loses every record modified
    # while it was running that it had already paged past. Setting it to the
    # start costs one re-read and loses nothing.
    clock, vendor, conn = _conn()
    started = clock.now()
    conn.backfill()
    finished = clock.now()
    assert finished > started
    assert conn.watermark == pytest.approx(started)
    assert conn.watermark < finished


def test_an_incremental_pass_advances_the_watermark_only_to_what_it_saw():
    # (mutation-checked: self.watermark = self.clock.now() at the end of
    # incremental_pass, the bug the comment there warns about, fails this.
    # A bare `watermark <= now` would not catch it: "advance to now"
    # satisfies "<= now" perfectly.)
    #
    # The gap is what matters. One record changes at t=10 and then nothing
    # happens for an hour. A pass at t=3610 has seen the world as of t=10 and
    # nothing later, so its watermark is 10. A pass that sets the watermark to
    # 3610 has asserted, without evidence, that it saw everything in between,
    # and every record stamped in that hour by a path it cannot scan is now
    # permanently below the bound.
    #
    # The timeline is EMPTY so the only change in the world is the one this
    # test makes. Against the generated timeline the two answers differ by
    # about a second, which is a difference a test can pass on by accident.
    clock, vendor, conn = _conn(SyncConfig(use_tiebreaker=True,
                                           inclusive_bound=True),
                                timeline=[])
    conn.backfill()
    assert conn.watermark == pytest.approx(0.0)

    clock.advance(10)
    rid = sorted(vendor.records)[0]
    vendor.records[rid]["version"] += 1
    vendor.records[rid]["last_modified"] = clock.now()
    changed_at = clock.now()

    clock.advance(3600)
    conn.incremental_pass()

    assert conn.store.records[rid]["version"] == vendor.records[rid]["version"]
    assert conn.watermark == pytest.approx(changed_at)
    assert conn.watermark < clock.now() - 3600


def test_a_quota_exhaustion_during_backfill_is_not_retried_forever():
    # A daily cap is not retryable. The run stops and records that it stopped.
    clock, vendor, conn = _conn(daily_allowance=5, per_second_limit=1000)
    conn.backfill()
    assert conn.quota_exhausted
    assert len(conn.store.records) < len(vendor.records)


def test_an_interrupted_backfill_leaves_no_watermark_behind():
    # (mutation-checked: set self.watermark = t0 unconditionally and this
    # fails, and so does the test below it, which shows what the watermark
    # would then cost)
    #
    # A backfill that stopped half way has not seen the records it never paged
    # to. Handing its successor a watermark asserts that it has.
    clock, vendor, conn = _conn(daily_allowance=5, per_second_limit=1000)
    r = conn.backfill()
    assert conn.quota_exhausted
    assert r["complete"] is False
    assert conn.backfill_complete is False
    assert conn.watermark is None
    assert r["watermark"] is None


def test_the_records_an_interrupted_backfill_missed_are_still_reachable():
    # What the line above prevents, run here. Every record in the generated
    # world carries a last_modified before the run starts, which is what makes
    # a first watermark meaningful, so a watermark of t0 would put every
    # unfetched record permanently below the incremental bound: invisible
    # rather than late, with no counter anywhere reporting a gap.
    clock, vendor, conn = _conn(daily_allowance=5, per_second_limit=1000)
    conn.backfill()
    held = len(conn.store.records)
    assert 0 < held < len(vendor.records)
    assert max(r["last_modified"] for r in vendor.records.values()) < 0

    # Tomorrow: the allowance is back and the caller carries on.
    vendor.daily_allowance = 100000
    vendor.quota_used = 0
    conn.quota_exhausted = False
    conn.config.use_tiebreaker = True
    conn.incremental_pass()
    assert len(conn.store.records) == len(vendor.records)


def test_a_finished_backfill_still_says_so_and_still_sets_the_watermark():
    # The other side, so that "complete" cannot be satisfied by always
    # reporting False.
    clock, vendor, conn = _conn()
    started = clock.now()
    r = conn.backfill()
    assert r["complete"] is True
    assert conn.backfill_complete is True
    assert conn.watermark == pytest.approx(started)
    assert r["watermark"] == pytest.approx(started)


# ---------------------------------------------------------------------------
# Scoring against the answer key
# ---------------------------------------------------------------------------

def test_score_counts_the_three_failure_shapes_apart():
    clock, vendor, conn = _conn()
    conn.backfill()
    s = score(conn.store, vendor)
    assert s["missing"] == 0
    assert s["ghost_records"] == 0
    assert s["accuracy"] == 1.0
    assert s["wrong"] == 0


def test_a_ghost_record_is_counted_when_upstream_deletes_it():
    clock, vendor, conn = _conn()
    conn.backfill()
    rid = sorted(vendor.records)[0]
    vendor._remove(vendor.records[rid], clock.now())
    s = score(conn.store, vendor)
    assert s["ghost_records"] == 1


def test_a_stale_record_is_counted_when_upstream_moves_on():
    clock, vendor, conn = _conn()
    conn.backfill()
    rid = sorted(vendor.records)[0]
    vendor.records[rid]["version"] += 1
    s = score(conn.store, vendor)
    assert s["stale"] == 1
    assert s["missing"] == 0


def test_scoring_by_kind_separates_what_the_aggregate_hides():
    # A single accuracy figure cannot distinguish random noise from one entire
    # class of change that no tuning will ever recover. That is the whole
    # reason this function exists.
    clock, vendor, conn = _conn(SyncConfig(use_tiebreaker=True,
                                           inclusive_bound=True,
                                           overlap_seconds=120.0,
                                           use_deletes_api=True))
    conn.backfill()
    conn.run(until=3600, interval_seconds=300)
    by_kind = score_by_mutation_kind(conn.store, vendor, vendor.timeline,
                                     up_to=clock.now())
    assert by_kind, "some mutations must have happened"
    for kind, rec in by_kind.items():
        assert rec["happened"] >= rec["reflected"] >= 0
        assert rec["missed"] == rec["happened"] - rec["reflected"]
        assert 0.0 <= rec["miss_rate"] <= 1.0


def test_a_silent_update_is_never_reflected_by_a_watermark_scan():
    # The central claim of the repository, asserted directly rather than
    # inferred from an aggregate. Every mitigation is switched on, and the
    # silent updates are still missed, because there is nothing in the wire
    # protocol that could reveal them.
    clock, vendor, conn = _conn(SyncConfig(use_tiebreaker=True,
                                           inclusive_bound=True,
                                           overlap_seconds=1800.0,
                                           use_deletes_api=True))
    conn.backfill()
    conn.run(until=14400, interval_seconds=300)
    by_kind = score_by_mutation_kind(conn.store, vendor, vendor.timeline,
                                     up_to=clock.now())
    silent = by_kind.get("SILENT_UPDATE")
    assert silent and silent["happened"] > 0
    assert silent["missed"] > 0
