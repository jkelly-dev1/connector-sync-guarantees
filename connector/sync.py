"""The connector: a local store and the sync strategies that fill it.

The ablation knobs are the experiment. Every mitigation that a real team
reaches for when incremental sync loses data is a parameter here, so the
question "does this actually help, and by how much" is answerable rather than
arguable:

    overlap_seconds     re-read a window before the watermark
    inclusive_bound     use >= rather than > at the boundary
    use_tiebreaker      order by (last_modified, id) rather than timestamp only
    use_deletes_api     consult the dedicated deletes endpoint
    scan_archived       treat an archived flag as a deletion signal

Two of those mitigations are free and are simply correct. Two of them cost
redundant reads. None of them recovers a silent update except by accident,
when its kept stamp falls inside the re-read window, and the experiment exists
to show which is which.
"""

from sim.vendors import QuotaExhausted, RateLimited


class Store:
    """The connector's local copy, and the bookkeeping that scores it.

    Upsert is keyed on the source id, never on a surrogate key and never on a
    natural key like a name. Retries, replayed webhooks and the deliberate
    overlap between backfill and incremental all deliver the same record twice,
    and an idempotent upsert is what makes every one of those a non-event
    rather than a duplicate.
    """

    def __init__(self):
        self.records = {}
        self.deleted = set()
        self.upserts = 0
        self.redundant_upserts = 0      # the cost of an overlap window
        self.delete_marks = 0

    def upsert(self, rec, source_version=None):
        rid = rec["id"]
        prior = self.records.get(rid)
        self.upserts += 1
        if prior is not None and prior.get("version") == rec.get("version"):
            # Same version as we already hold: this read told us nothing new.
            # Counting these is how the overlap window's cost becomes visible.
            self.redundant_upserts += 1
        # A version check, not a blind write. An out-of-order delivery must not
        # revert a record to an older state; without this, event 1 arriving
        # after event 2 silently undoes event 2.
        if prior is not None and source_version is not None:
            if prior.get("version", 0) > source_version:
                return False
        self.records[rid] = dict(rec)
        self.deleted.discard(rid)
        return True

    def mark_deleted(self, rid):
        if rid in self.records and rid not in self.deleted:
            self.delete_marks += 1
        self.deleted.add(rid)

    def live(self):
        return {k: v for k, v in self.records.items() if k not in self.deleted}

    def stats(self):
        return {"records_held": len(self.records),
                "live_records": len(self.live()),
                "marked_deleted": len(self.deleted),
                "upserts": self.upserts,
                "redundant_upserts": self.redundant_upserts}


class SyncConfig:
    """Every knob the incremental sync has, with the defaults a team starts at.

    The defaults are deliberately the naive ones. Overlap 0, strict >, no
    tiebreaker, no deletes call. That is what "we do incremental sync on
    last_modified" means when nobody has been paged yet, the
    configuration experiment 2 scores first.
    """

    def __init__(self, overlap_seconds=0.0, inclusive_bound=False,
                 use_tiebreaker=False, use_deletes_api=False,
                 scan_archived=False, page_size=200):
        self.overlap_seconds = float(overlap_seconds)
        self.inclusive_bound = bool(inclusive_bound)
        self.use_tiebreaker = bool(use_tiebreaker)
        self.use_deletes_api = bool(use_deletes_api)
        self.scan_archived = bool(scan_archived)
        self.page_size = int(page_size)

    def as_dict(self):
        return {"overlap_seconds": self.overlap_seconds,
                "inclusive_bound": self.inclusive_bound,
                "use_tiebreaker": self.use_tiebreaker,
                "use_deletes_api": self.use_deletes_api,
                "scan_archived": self.scan_archived,
                "page_size": self.page_size}


class Connector:
    """Backfill and incremental sync against one vendor."""

    def __init__(self, vendor, clock, limiter, store=None, config=None):
        self.vendor = vendor
        self.clock = clock
        self.limiter = limiter
        self.store = store or Store()
        self.config = config or SyncConfig()
        self.watermark = None
        self.passes = 0
        self.backfill_complete = None    # None = no backfill has been run
        self.quota_exhausted = False
        self.throttle_events = 0
        self.deletes_failures = 0

    # ---- the call wrapper -------------------------------------------------

    def _call(self, fn, *args, **kwargs):
        """One vendor call, paced by the limiter and retried on a 429.

        A daily cap is not retried. Retrying an exhausted allowance is pure
        waste, and on a vendor where rejected calls still cost quota it is
        actively harmful. The two failures get different handling because they
        are different problems.
        """
        while True:
            self.limiter.acquire()
            try:
                out = fn(*args, **kwargs)
            except RateLimited as exc:
                self.throttle_events += 1
                self.limiter.on_throttled(exc.retry_after)
                continue
            except QuotaExhausted:
                self.quota_exhausted = True
                raise
            self.limiter.on_success()
            return out

    # ---- backfill ---------------------------------------------------------

    def backfill(self, max_pages=100000):
        """Page through everything, recording the start time FIRST.

        The watermark comes from the start of the backfill, not the end. A
        backfill that runs for hours and then sets the watermark to its finish
        time permanently loses every record modified while it was running that
        it had already paged past. Setting it to the start and relying on an
        idempotent upsert to absorb the overlap is the whole fix, and it costs
        one re-read.

        A backfill that did not finish sets no watermark at all. The pages
        it never reached hold records whose last_modified is BELOW t0, so a
        caller that carries on incrementally from t0 will never ask for them
        again: they are invisible rather than late, and nothing downstream
        reports a gap. Leaving the watermark unset makes the next pass a full
        scan, which is the only safe thing an interrupted backfill can hand its
        successor. The caller is told either way, in `complete`.
        """
        t0 = self.clock.now()
        cursor = None
        pages = 0
        complete = True
        while pages < max_pages:
            try:
                page = self._call(self.vendor.query_modified_since,
                                  since=-1e18, limit=self.config.page_size,
                                  last_id=cursor)
            except QuotaExhausted:
                complete = False
                break
            if not page:
                break
            for rec in page:
                self.store.upsert(rec, source_version=rec.get("version"))
            last = page[-1]
            cursor = (last["last_modified"], last["id"])
            pages += 1
        else:
            # Ran out of the page budget rather than out of records: there may
            # be more upstream, so this is not a finished backfill either.
            complete = False
        self.backfill_complete = complete
        if complete:
            self.watermark = t0
        return {"pages": pages, "watermark": self.watermark,
                "complete": complete}

    # ---- incremental ------------------------------------------------------

    def incremental_pass(self):
        """One incremental sync pass.

        Returns the number of records observed. The ablation knobs decide how
        many of the changes that actually happened this pass is capable of
        seeing at all.
        """
        self.passes += 1
        cfg = self.config
        if self.watermark is None:
            # No watermark is not a watermark of zero. Nothing has been
            # established as seen, so the only correct lower bound is
            # everything, which is what an interrupted backfill hands its
            # successor and why it refuses to leave one behind. Reading an
            # unset watermark as 0.0 would put every record stamped before the
            # run began permanently below the bound.
            since = -1e18
        else:
            since = self.watermark - cfg.overlap_seconds
            if not cfg.inclusive_bound:
                # Strict greater-than, the naive default. Records sharing the
                # exact boundary timestamp are skipped. With a coarse clock or
                # a bulk write that stamps many rows identically, that is not a
                # rare edge case.
                since = since + 1e-9

        observed = 0
        highest = self.watermark
        cursor = None
        while True:
            try:
                page = self._call(self.vendor.query_modified_since,
                                  since=since, limit=cfg.page_size,
                                  last_id=cursor if cfg.use_tiebreaker else None)
            except QuotaExhausted:
                break
            if not page:
                break
            for rec in page:
                if rec.get("archived") and cfg.scan_archived:
                    self.store.mark_deleted(rec["id"])
                else:
                    self.store.upsert(rec, source_version=rec.get("version"))
                observed += 1
                if highest is None or rec["last_modified"] > highest:
                    highest = rec["last_modified"]
            if not cfg.use_tiebreaker:
                # Without a tiebreaker there is no safe cursor. The only thing
                # available is the timestamp, and re-querying from the last
                # timestamp seen either loops forever on a tie group or skips
                # it. The realistic naive implementation takes the first page
                # and stops, which is exactly the silent truncation this models.
                break
            last = page[-1]
            cursor = (last["last_modified"], last["id"])
            if len(page) < cfg.page_size:
                break

        deletes_failed = False
        if cfg.use_deletes_api:
            try:
                gone = self._call(
                    self.vendor.get_deleted,
                    since=(-1e18 if self.watermark is None
                           else self.watermark - cfg.overlap_seconds),
                    until=self.clock.now())
                for rid in gone:
                    self.store.mark_deleted(rid)
            except (QuotaExhausted, NotImplementedError):
                # The deletions in this window were not read. Leaving the
                # watermark where it was makes the next pass ask again;
                # advancing it would put them behind the cursor for good.
                deletes_failed = True
                self.deletes_failures += 1

        if deletes_failed:
            return observed

        # Advance to what was observed, not to "now". Advancing to the current
        # time asserts that everything up to now was seen, which is precisely
        # the assumption that fails. A pass that observed nothing establishes
        # nothing, so it leaves the watermark exactly where it found it.
        self.watermark = highest
        return observed

    def run(self, until, interval_seconds):
        """Run incremental passes on a schedule until the simulated deadline."""
        while self.clock.now() < until:
            self.vendor.apply_timeline_to_now()
            self.incremental_pass()
            remaining = until - self.clock.now()
            if remaining <= 0:
                break
            self.clock.advance(min(interval_seconds, remaining))
        self.vendor.apply_timeline_to_now()
        return self.passes


def score(store, vendor):
    """Compare the connector's local copy against the vendor's actual state.

    This is the answer key in use. The vendor knows what is true; the store
    knows what the connector believes. Every number below is a difference
    between the two, per record, with no estimation anywhere.
    """
    truth_live = {r["id"]: r for r in vendor._live()}
    local_live = store.live()

    missing = 0            # exists upstream, absent locally
    stale = 0              # present locally but with an older version
    correct = 0
    ghost = 0              # gone upstream, still live locally

    for rid, rec in truth_live.items():
        local = local_live.get(rid)
        if local is None:
            missing += 1
        elif local.get("version") != rec.get("version"):
            stale += 1
        else:
            correct += 1

    for rid in local_live:
        if rid not in truth_live:
            ghost += 1

    total = len(truth_live)
    return {
        "upstream_live": total,
        "local_live": len(local_live),
        "correct": correct,
        "stale": stale,
        "missing": missing,
        "ghost_records": ghost,
        "accuracy": round(correct / total, 6) if total else None,
        "wrong": stale + missing + ghost,
    }


def attribute_wrong_records(store, vendor, timeline, up_to):
    """For each record that ends up WRONG, which kind of change is to blame.

    Why this exists and why score_by_mutation_kind IS NOT ENOUGH. That function
    asks, per mutation, "is the record it touched correct at the end". A record
    that received an ordinary UPDATE and later a SILENT_UPDATE is wrong at the
    end, so BOTH mutations are counted as missed, and the ordinary update gets
    blamed for a failure that belongs entirely to the silent one. Summing those
    per-kind misses therefore over-counts: 130 mutation-level misses on 58
    wrong records.

    This attributes each WRONG RECORD to exactly one kind: the LATEST mutation
    on it that the connector failed to reflect. The counts sum to the number of
    wrong records, which is the number a reader actually cares about.
    """
    truth_live = {r["id"]: r for r in vendor._live()}
    local_live = store.live()

    wrong_ids = set()
    for rid, rec in truth_live.items():
        local = local_live.get(rid)
        if local is None or local.get("version") != rec.get("version"):
            wrong_ids.add(rid)
    for rid in local_live:
        if rid not in truth_live:
            wrong_ids.add(rid)

    latest = {}
    for m in timeline:
        if m["at"] > up_to:
            continue
        if m["record_id"] in wrong_ids:
            latest[m["record_id"]] = m

    by_kind = {}
    for rid, m in latest.items():
        by_kind[m["kind"]] = by_kind.get(m["kind"], 0) + 1
    return {"wrong_records": len(wrong_ids),
            "attributed_to_kind": by_kind,
            "unattributed": len(wrong_ids) - len(latest)}


def score_by_mutation_kind(store, vendor, timeline, up_to):
    """Which KINDS of change the connector failed to observe.

    Read the caveat. This is a per-MUTATION view: it asks, for each change,
    whether the record it touched is correct at the end of the run. A record
    touched several times is therefore counted once per mutation, so these
    numbers OVER-COUNT relative to the number of wrong records. Use
    attribute_wrong_records() for the per-record view that sums correctly.

    The aggregate hides the finding. A single "97% accurate" figure says
    nothing about whether the missing 3% is random noise or one entire class of
    change that no amount of tuning will ever recover. This groups every
    mutation by kind and asks, for each, whether the connector ended up holding
    the right value.
    """
    seen = {}
    for m in timeline:
        if m["at"] > up_to:
            continue
        kind = m["kind"]
        rec = seen.setdefault(kind, {"happened": 0, "reflected": 0})
        rec["happened"] += 1

        rid = m["record_id"]
        truth = vendor.records.get(rid)
        local = store.records.get(rid)

        if kind in ("DELETE", "MERGE"):
            # Reflected means the connector knows it is gone.
            if rid in store.deleted or local is None:
                rec["reflected"] += 1
        elif truth is None or vendor._tombstoned(truth):
            # The record has since been deleted or archived upstream, so this
            # change has no current value to reflect. Scoring it as missed
            # would charge a correct delete mechanism for every update that
            # preceded the delete. It is counted apart instead.
            rec["happened"] -= 1
            rec["superseded_by_delete"] = rec.get("superseded_by_delete", 0) + 1
        else:
            if truth is not None and local is not None \
                    and local.get("version") == truth.get("version"):
                rec["reflected"] += 1

    for kind, rec in seen.items():
        rec["missed"] = rec["happened"] - rec["reflected"]
        rec["miss_rate"] = round(rec["missed"] / rec["happened"], 6) \
            if rec["happened"] else 0.0
    return seen
