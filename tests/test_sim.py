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


# Modules and attributes that would make a published figure depend on when or
# where it was computed. `secrets` and `os.urandom` are entropy rather than
# time, and belong here for the same reason: a figure that moves between runs
# is not a result.
_WALL_CLOCK_MODULES = {"time", "datetime", "random", "secrets"}
_WALL_CLOCK_ATTRS = {("os", "urandom"), ("os", "getrandom"),
                     ("os", "times"), ("os", "clock")}


def _wall_clock_reads(path):
    """Every wall-clock or entropy access in one file, found by PARSING it."""
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _WALL_CLOCK_MODULES:
                    found.append("%s:%d import %s"
                                 % (path.name, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _WALL_CLOCK_MODULES:
                names = ", ".join(a.name for a in node.names)
                found.append("%s:%d from %s import %s"
                             % (path.name, node.lineno, node.module, names))
        elif isinstance(node, ast.Attribute) and isinstance(node.value,
                                                            ast.Name):
            if (node.value.id, node.attr) in _WALL_CLOCK_ATTRS:
                found.append("%s:%d %s.%s" % (path.name, node.lineno,
                                              node.value.id, node.attr))
    return found


def _measurement_path():
    """Every module a published figure is computed by."""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    files = []
    for folder in ("sim", "connector", "scripts"):
        files += sorted(p for p in (repo / folder).glob("*.py")
                        if not p.name.startswith("_") or p.name == "__init__.py")
    return files


def test_nothing_in_the_simulation_reads_the_clock_or_a_random_stream():
    # (mutation-checked: add `from time import time` to sim/vendors.py and it
    # fails)
    #
    # Why it parses instead of searching for "time.time" or "random.":
    #   - `from time import time; time()` contains none of those substrings;
    #   - a clock read in the vendor or the connector moves a published figure
    #     exactly as much as one in the generator, so every module in the
    #     measurement path is read, not only sim.world.
    # An import is a structural fact, so it is found structurally.
    files = _measurement_path()
    # A filter that quietly matched nothing would make this test green while
    # examining zero files, which is the failure mode of every check that
    # narrows its own input.
    assert len(files) >= 15, [f.name for f in files]
    assert {f.name for f in files} >= {"world.py", "vendors.py", "clock.py",
                                       "webhooks.py", "limiters.py", "sync.py"}
    found = []
    for path in files:
        found += _wall_clock_reads(path)
    assert found == [], found


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


def test_a_by_id_fetch_does_not_hand_back_a_deleted_record():
    # (mutation-checked: drop the tombstone filter in Vendor.get_many and this
    # fails on both vendors)
    # The ONLY signal a by-id reader gets about a deletion is ABSENCE. Asking
    # either shape for a record it has deleted or archived answers NOT FOUND;
    # the row is reachable only by explicitly asking for deleted or archived
    # rows, which is what get_deleted() and _scannable() are for. A fake that
    # returned the tombstone with a flag on it would let a connector re-upsert
    # a deleted record as live and still score as correct, through the
    # webhook arm of experiment 3.
    c = Clock()
    v = Atlas(c)
    rid = sorted(v.records)[0]
    assert [r["id"] for r in v.get_many([rid])] == [rid]
    v._remove(v.records[rid], c.now())
    assert v.get_many([rid]) == []
    # Still in the vendor's own table, and still findable the documented way.
    assert rid in v.records
    assert rid in v.get_deleted(-1, c.now())


def test_a_by_id_fetch_does_not_hand_back_an_archived_record():
    # Beacon archives rather than deletes, and the archived row REMAINS
    # SCANNABLE. That is the one asymmetry the whole repository is about, and
    # it must not leak into the by-id path: scannable is not the same as
    # readable by id.
    c = Clock()
    v = Beacon(c)
    rid = sorted(v.records)[0]
    assert [r["id"] for r in v.get_many([rid])] == [rid]
    v._remove(v.records[rid], c.now())
    assert v.get_many([rid]) == []
    assert rid in {r["id"] for r in v._scannable()}


def test_a_vendor_that_does_not_say_what_a_tombstone_is_fails_loudly():
    # (mutation-checked: `return False` in Vendor._tombstoned and this passes
    # while every read path on such a vendor silently treats deleted rows as
    # live, including _live(), which is the answer key the whole scoring
    # function is built on)
    #
    # The base class must not guess. Atlas spells a tombstone `deleted` and
    # Beacon spells it `archived`; a third shape that forgets to say gets an
    # exception at its first read, not a quietly wrong world.
    from sim.vendors import Vendor

    class Nameless(Vendor):
        name = "nameless"

        def _remove(self, rec, at):
            rec["deleted"] = True

    c = Clock()
    v = Nameless(c, records=dict(W.initial_world("atlas")), timeline=[])
    # The message is asserted as well as the type. A bare `raises` is satisfied
    # by any NotImplementedError, including one raised by a method this test is
    # not about: `_remove` and `get_deleted` are both stubs on this class.
    with pytest.raises(NotImplementedError, match="does not say what a "
                       "tombstone looks like"):
        v._live()
    with pytest.raises(NotImplementedError, match="does not say what a "
                       "tombstone looks like"):
        v.get_many([sorted(v.records)[0]])


def test_a_by_id_fetch_still_charges_for_the_call_that_found_nothing():
    # A NOT FOUND is a round trip. If the tombstone filter were implemented by
    # skipping the call, the N+1 access pattern in experiment 1 would get
    # cheaper every time the world deleted something.
    c = Clock()
    v = Atlas(c)
    rid = sorted(v.records)[0]
    v._remove(v.records[rid], c.now())
    before = v.calls
    v.get_many([rid])
    assert v.calls == before + 1


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


def _tied_records(n=9, at=5.0):
    """n records that all share one last_modified, inserted in an order that is
    not id order.

    The insertion order matters. Python's sort is stable, so a sort by
    timestamp alone leaves a tie group in insertion order, and a fixture
    built in id order therefore comes back correctly sorted from a vendor with
    no tiebreaker at all. Such a fixture cannot express the violation it is
    there to catch.
    """
    ids = ["A-%06d" % i for i in range(1, n + 1)]
    scrambled = ids[n // 2:] + ids[:n // 2]
    return {rid: {"id": rid, "name": rid, "owner": "alice", "stage": "new",
                  "amount": 1000, "region": "amer", "last_modified": at,
                  "deleted": False, "archived": False, "merged_into": None,
                  "version": 1}
            for rid in scrambled}


def test_the_generated_world_really_does_contain_ties():
    # (mutation-checked: shrink N_RECORDS until collisions stop happening)
    # The mechanism below is only worth pinning if the shipped corpus can hit
    # it. It can: the timestamps are drawn from a bounded integer range over
    # 4,000 records, so collisions are not an edge case.
    lm = [r["last_modified"] for r in W.initial_world("atlas").values()]
    assert len(lm) > len(set(lm))


def test_the_scan_breaks_a_tie_by_id_and_not_by_insertion_order():
    # (mutation-checked: sort by r["last_modified"] alone in
    # query_modified_since and this fails, and so does the paging test below.
    # The previous fixture was the shipped 4,000-record world, whose first
    # 50 rows contain no tie at all, so the mutation left the page BYTE
    # IDENTICAL and the test passed.)
    c = Clock()
    v = Atlas(c, records=_tied_records(), timeline=[])
    page = v.query_modified_since(-1e18, limit=50)
    assert len({r["last_modified"] for r in page}) == 1, "the fixture has no tie"
    assert [r["id"] for r in page] == sorted(r["id"] for r in page)


def test_paging_across_a_tie_group_returns_every_record_exactly_once():
    # The consequence, not the property. A page boundary that lands inside a
    # tie group is where a timestamp-only cursor loses records, and it loses
    # them silently: the response looks like an ordinary page.
    c = Clock()
    v = Atlas(c, records=_tied_records(n=9), timeline=[])
    seen = []
    cursor = None
    while True:
        page = v.query_modified_since(-1e18, limit=2, last_id=cursor)
        if not page:
            break
        seen += [r["id"] for r in page]
        last = page[-1]
        cursor = (last["last_modified"], last["id"])
    assert len(seen) == len(set(seen)), "a record was returned twice"
    assert sorted(seen) == sorted(v.records), "a record was never returned"


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
