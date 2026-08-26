"""The simulation: the clock, the generated world, and the limiters.

Every test here is a pure function against a simulated Clock. Nothing sleeps,
nothing opens a socket, and nothing depends on how fast this machine is, which
is the same property that makes the published figures reproducible.
"""

import pytest

from sim import limiters, world as W
from sim.clock import Clock, Skew
from sim.vendors import Atlas, Beacon, QuotaExhausted, RateLimited


# ---------------------------------------------------------------------------
# THE CLOCK
# ---------------------------------------------------------------------------

def test_the_clock_refuses_to_go_backward():
    # (mutation-checked: drop the guard and an ordering bug becomes
    # unreproducible instead of raising at the point of failure)
    c = Clock()
    c.advance(10)
    with pytest.raises(ValueError):
        c.advance(-1)
    assert c.now() == 10


def test_the_clock_refuses_a_nan_advance():
    c = Clock()
    with pytest.raises(ValueError):
        c.advance(float("nan"))


def test_advance_to_is_idempotent_and_never_rewinds():
    c = Clock()
    c.advance_to(100)
    c.advance_to(50)          # already past it
    assert c.now() == 100


def test_a_skewed_clock_reports_a_different_now():
    # The mechanism behind one of the four watermark losses. A vendor stamping
    # from a clock behind yours writes records that are already below a
    # watermark taken from your clock.
    c = Clock()
    c.advance(1000)
    behind = Skew(c, -11.0)
    assert behind.now() == 989.0
    assert c.now() == 1000.0


# ---------------------------------------------------------------------------
# THE GENERATED WORLD
# ---------------------------------------------------------------------------

def test_the_world_is_a_pure_function_of_the_seed():
    assert W.manifest()["sha256"] == W.manifest()["sha256"]
    assert W.initial_record("atlas", 7) == W.initial_record("atlas", 7)


def test_nothing_in_the_generator_reads_the_clock_or_a_random_stream():
    # Checked by source rather than behavior: an occasional clock read would
    # pass a behavioral check almost every time.
    import inspect
    src = inspect.getsource(W)
    for forbidden in ("time.time", "datetime.now", "random.", "os.urandom"):
        assert forbidden not in src, forbidden


def test_two_different_coordinates_cannot_collide_into_one_draw():
    assert W._bits("a", "bc") != W._bits("ab", "c")


def test_no_mutation_carries_a_timestamp_from_the_future():
    # (mutation-checked: build the timeline in sequence order and sort by time
    # afterwards, and a silent update copies a stamp from a mutation that has
    # not happened yet. The watermark then jumps to the end of the timeline
    # and incremental sync is dead for the rest of the run, while still
    # reporting a plausible accuracy)
    for vendor in ("atlas", "beacon"):
        for m in W.mutation_timeline(vendor):
            assert m["stamp"] <= m["at"] + 1e-6, m


def test_the_timeline_is_ordered_by_time():
    tl = W.mutation_timeline("atlas")
    assert [m["at"] for m in tl] == sorted(m["at"] for m in tl)


def test_a_silent_update_does_not_advance_the_stamp():
    # The unfixable loss, asserted at its source. If this ever starts advancing
    # the stamp, experiment 2's central finding quietly disappears.
    tl = W.mutation_timeline("atlas")
    silent = [m for m in tl if m["kind"] == "SILENT_UPDATE"]
    assert silent, "the world must contain silent updates"
    for m in silent:
        assert m["stamp"] < m["at"]


def test_a_late_clock_update_is_stamped_behind_its_moment():
    tl = W.mutation_timeline("atlas")
    late = [m for m in tl if m["kind"] == "LATE_CLOCK_UPDATE"]
    assert late
    for m in late:
        assert m["stamp"] == pytest.approx(m["at"] + W.VENDOR_CLOCK_SKEW)


def test_every_mutation_kind_in_the_mix_actually_occurs():
    # A kind that never fires is a kind the experiments cannot measure.
    kinds = {m["kind"] for m in W.mutation_timeline("atlas")}
    for kind, _ in W.MUTATION_MIX:
        assert kind in kinds, kind


def test_a_deleted_record_is_never_mutated_again():
    tl = W.mutation_timeline("atlas")
    dead = set()
    for m in tl:
        assert m["record_id"] not in dead, m
        if m["kind"] in ("DELETE", "MERGE"):
            dead.add(m["record_id"])


# ---------------------------------------------------------------------------
# THE VENDORS
# ---------------------------------------------------------------------------

def test_every_call_costs_simulated_time():
    # (mutation-checked: make a call free and the naive limiter completes in
    # 0.0 simulated seconds while issuing 100,000 requests, which makes
    # retry-immediately look infinitely fast)
    c = Clock()
    v = Atlas(c)
    before = c.now()
    v.count()
    assert c.now() > before


def test_a_rejected_call_costs_time_too():
    c = Clock()
    v = Atlas(c)
    v.per_second_limit = 1
    v.count()
    before = c.now()
    with pytest.raises(RateLimited):
        for _ in range(5):
            v.count()
    assert c.now() > before


def test_a_daily_cap_raises_a_different_exception_from_a_rate_limit():
    # They are different problems. A rate limit fixes itself in a second; a cap
    # does not fix itself today. Collapsing them is how a connector spends the
    # rest of the day retrying something that cannot succeed.
    c = Clock()
    v = Atlas(c)
    v.daily_allowance = 2
    v.per_second_limit = 1000
    v.count()
    v.count()
    with pytest.raises(QuotaExhausted):
        v.count()
    assert not issubclass(QuotaExhausted, RateLimited)


def test_atlas_hard_deletes_and_beacon_archives():
    # The single difference that makes delete detection easy on one vendor and
    # impossible-without-a-special-endpoint on the other.
    ca, a = Clock(), None
    a = Atlas(ca)
    rid = sorted(a.records)[0]
    a._remove(a.records[rid], 0.0)
    assert rid not in {r["id"] for r in a._live()}
    assert rid not in {r["id"] for r in a._scannable()}

    cb = Clock()
    b = Beacon(cb)
    rid = sorted(b.records)[0]
    b._remove(b.records[rid], 0.0)
    assert rid not in {r["id"] for r in b._live()}
    # Still visible to the scan. That is what archive semantics buy.
    assert rid in {r["id"] for r in b._scannable()}


def test_the_deletes_endpoint_forgets_beyond_its_retention_window():
    # A caller asking about a period older than retention gets an empty answer
    # that is indistinguishable from "nothing was deleted".
    c = Clock()
    v = Atlas(c)
    v.delete_retention_seconds = 100.0
    rid = sorted(v.records)[0]
    v._remove(v.records[rid], 0.0)
    c.advance(50)
    assert rid in v.get_deleted(-1, c.now())
    c.advance(500)
    assert rid not in v.get_deleted(-1, c.now())


def test_the_scan_orders_by_timestamp_then_id():
    # Without a tiebreaker there is no total order, and paging across a tie
    # group skips records at every boundary that lands inside one.
    c = Clock()
    v = Atlas(c)
    page = v.query_modified_since(-1e18, limit=50)
    keys = [(r["last_modified"], r["id"]) for r in page]
    assert keys == sorted(keys)


def test_beacon_search_truncates_silently_at_its_ceiling():
    c = Clock()
    v = Beacon(c)
    v.search_result_ceiling = 10
    v.page_size_max = 100
    rows = v.search_modified_since(-1e18, limit=100)
    assert len(rows) <= 10


# ---------------------------------------------------------------------------
# THE LIMITERS
# ---------------------------------------------------------------------------

def test_a_token_bucket_paces_to_its_rate():
    c = Clock()
    lim = limiters.TokenBucket(c, rate=2.0, capacity=1.0)
    lim.acquire()
    start = c.now()
    for _ in range(4):
        lim.acquire()
    # Four more tokens at 2/sec is about two seconds of simulated waiting.
    assert c.now() - start == pytest.approx(2.0, abs=0.01)


def test_a_token_bucket_permits_a_burst_up_to_its_capacity():
    c = Clock()
    lim = limiters.TokenBucket(c, rate=1.0, capacity=5.0)
    start = c.now()
    for _ in range(5):
        lim.acquire()
    assert c.now() == start          # the burst is free


def test_the_naive_limiter_never_waits():
    c = Clock()
    lim = limiters.Naive(c)
    start = c.now()
    for _ in range(100):
        lim.acquire()
    assert c.now() == start


def test_aimd_decreases_multiplicatively_and_increases_additively():
    c = Clock()
    lim = limiters.AIMD(c, start_rate=8.0, max_rate=16.0)
    lim.on_throttled(0.0)
    assert lim.rate == pytest.approx(4.0)      # halved
    lim.on_success()
    assert lim.rate == pytest.approx(4.5)      # +0.5, not doubled


def test_aimd_never_falls_below_its_floor():
    c = Clock()
    lim = limiters.AIMD(c, start_rate=1.0, max_rate=10.0, min_rate=0.25)
    for _ in range(20):
        lim.on_throttled(0.0)
    assert lim.rate >= 0.25


def test_a_limiter_honors_retry_after_rather_than_its_own_backoff():
    # The server knows when it will accept the next call. Substituting your own
    # guess is choosing to be wrong about the one number it actually knows.
    c = Clock()
    lim = limiters.TokenBucket(c, rate=100.0, capacity=100.0)
    start = c.now()
    lim.on_throttled(7.5)
    assert c.now() - start == pytest.approx(7.5)


def test_build_refuses_an_unknown_strategy():
    with pytest.raises(ValueError):
        limiters.build("exponential_hope", Clock(), 10.0)


def test_every_named_strategy_can_be_built():
    c = Clock()
    for name in limiters.STRATEGIES:
        lim = limiters.build(name, c, documented_rate=10.0, share=0.5)
        assert lim.name == name
