"""Two mock external APIs with deliberately different constraint shapes.

These are fakes of a shape, not reimplementations of a product. Atlas is
modeled on how a Salesforce-shaped system behaves and Beacon on a
HubSpot-shaped one, taken from the publicly documented behavior of systems of
that kind. No real API is called, no account is used, and nothing here is
evidence about any real product. The asymmetry between the two is the point: a
connector tuned for one is measurably wrong for the other.

  ATLAS (Salesforce-shaped)
    a DAILY CALL ALLOWANCE plus a modest per-second cap
    an async bulk export job: cheap per record, expensive in latency
    a modified-since query, ordered, with a tiebreaker
    a dedicated deletes endpoint with a RETENTION WINDOW
    hard deletes

  BEACON (HubSpot-shaped)
    a BURST limit over a short rolling window plus a daily cap
    a search endpoint with its OWN tighter limit and a RESULT CEILING that
      silently truncates deep result sets
    small synchronous batch endpoints
    ARCHIVE semantics: a deleted record stays readable with a flag

The Vendor enforces its own limits. The connector's limiter is a client-side
guess; this class is the ground truth that issues the 429s. Keeping the two
separate is what makes the limiter comparison meaningful; otherwise the
experiment would be measuring a limiter against itself.
"""

from sim import world as W


class RateLimited(Exception):
    """Raised by a vendor when the caller exceeded a limit."""

    def __init__(self, retry_after=None, kind="rate"):
        super().__init__("rate limited (%s)" % kind)
        self.retry_after = retry_after
        self.kind = kind


class QuotaExhausted(Exception):
    """The DAILY allowance is gone. Retrying today cannot help.

    A separate exception type on purpose. A per-second limit is a throughput
    problem that fixes itself in a second; a daily cap is a capacity problem
    that nothing fixes until tomorrow. Collapsing them into one error is how a
    connector spends the rest of its day retrying something that cannot
    succeed.
    """


class Vendor:
    """Shared machinery: the record store, the timeline, and quota accounting."""

    name = "vendor"
    daily_allowance = 100000
    per_second_limit = 10.0
    # Whether a rejected call still costs quota. Several real platforms count
    # rejected calls, which makes naive retry actively harmful rather than
    # merely wasteful.
    throttled_calls_count_against_quota = True
    page_size_max = 200
    delete_retention_seconds = None      # None = deletes visible forever
    search_result_ceiling = None         # None = no ceiling
    # every call costs time, including a rejected one. A round trip happens
    # whether the answer is data or a 429, and a simulation where a rejected
    # call is free makes retry-immediately look infinitely fast: the naive
    # limiter completes in zero simulated seconds while issuing a hundred
    # thousand requests. Charging the round trip is what makes the strategy
    # comparison mean anything.
    call_latency_seconds = 0.05

    def __init__(self, clock, records=None, timeline=None):
        self.clock = clock
        self.records = records if records is not None else W.initial_world(self.name)
        self.timeline = timeline if timeline is not None else W.mutation_timeline(self.name)
        self._applied = 0                # how far through the timeline we are
        self.deleted_log = []            # (record_id, at) for the deletes API
        # Accounting
        self.calls = 0
        self.calls_throttled = 0
        self.calls_by_endpoint = {}
        self.quota_used = 0
        self.records_returned = 0
        self._recent = []                # call timestamps, for the rate window

    # ---- time and mutation ------------------------------------------------

    def apply_timeline_to_now(self):
        """Apply every mutation whose moment has arrived.

        The vendor's state advances with the clock, not with the connector's
        requests. That is what makes an in-flight update possible: the world
        changes underneath a paging scan.
        """
        now = self.clock.now()
        while (self._applied < len(self.timeline)
               and self.timeline[self._applied]["at"] <= now):
            self._apply(self.timeline[self._applied])
            self._applied += 1

    def _apply(self, m):
        rec = self.records.get(m["record_id"])
        if rec is None:
            return
        kind = m["kind"]
        if kind == "DELETE":
            self._remove(rec, m["at"])
        elif kind == "MERGE":
            self._remove(rec, m["at"])
            rec["merged_into"] = m["merged_into"]
            survivor = self.records.get(m["merged_into"])
            if survivor is not None:
                survivor["last_modified"] = m["at"]
                survivor["version"] += 1
        else:
            rec[m["field"]] = m["value"]
            rec["version"] += 1
            # The stamp is not always the moment. This single line is where
            # every watermark loss in this repository comes from.
            if m["stamp"] > rec["last_modified"]:
                rec["last_modified"] = m["stamp"]

    def _remove(self, rec, at):
        raise NotImplementedError

    # ---- quota ------------------------------------------------------------

    def _charge(self, endpoint, cost=1):
        self.calls += cost
        self.calls_by_endpoint[endpoint] = self.calls_by_endpoint.get(endpoint, 0) + cost
        self.quota_used += cost

    def _check_limits(self, endpoint, cost=1):
        """Enforce the vendor's own limits. Raises, or returns cleanly.

        The round trip is charged FIRST, before any limit is evaluated,
        because the caller pays for it either way.
        """
        self.clock.advance(self.call_latency_seconds)
        self.apply_timeline_to_now()

        if self.quota_used + cost > self.daily_allowance:
            if self.throttled_calls_count_against_quota:
                self.quota_used += cost
            self.calls_throttled += 1
            raise QuotaExhausted(
                "daily allowance of %d exhausted" % self.daily_allowance)

        now = self.clock.now()
        window = self._rate_window()
        self._recent = [t for t in self._recent if t > now - window]
        if len(self._recent) >= self._rate_ceiling():
            self.calls_throttled += 1
            if self.throttled_calls_count_against_quota:
                self._charge(endpoint + ":throttled", cost)
            oldest = min(self._recent)
            raise RateLimited(retry_after=max(0.001, oldest + window - now),
                              kind="rate")
        self._recent.append(now)
        self._charge(endpoint, cost)

    def _rate_window(self):
        return 1.0

    def _rate_ceiling(self):
        return self.per_second_limit

    def usage(self):
        return {
            "vendor": self.name,
            "calls": self.calls,
            "calls_throttled": self.calls_throttled,
            "quota_used": self.quota_used,
            "daily_allowance": self.daily_allowance,
            "quota_share_used": round(self.quota_used / self.daily_allowance, 6),
            "records_returned": self.records_returned,
            "calls_by_endpoint": dict(self.calls_by_endpoint),
        }

    # ---- the read API -----------------------------------------------------

    def _tombstoned(self, rec):
        """Whether this row is a tombstone: deleted, or archived, upstream.

        Each vendor shape spells the tombstone differently (Atlas sets a
        `deleted` flag, Beacon an `archived` one), and every read path that
        must not hand a tombstone back asks this predicate instead of testing
        the flag itself. Having one predicate is what stops a new endpoint from
        accidentally being the one that leaks them.
        """
        raise NotImplementedError(
            "%s does not say what a tombstone looks like; every read path "
            "depends on it" % type(self).__name__)

    def _live(self):
        return [r for r in self.records.values() if not self._tombstoned(r)]

    def _scannable(self):
        """The records the modified-since scan can see.

        Defaults to the live set, which is what a hard-delete vendor offers: a
        deleted record is simply gone and no scan will ever mention it again. A
        vendor with ARCHIVE semantics overrides this to include the archived
        rows, which is precisely why archive semantics are easier to integrate
        against: deletion detection falls out of the ordinary scan instead of
        needing a dedicated endpoint with a retention window.
        """
        return self._live()

    def query_modified_since(self, since, limit=None, last_id=None):
        """The modified-since scan, ordered by (last_modified, id).

        The tiebreaker is part of the contract. Ordering by a timestamp alone
        is not a total order, and paging across a tie group without a
        tiebreaker skips records at every page boundary that lands inside one.
        A vendor that offers no tiebreaker cannot be safely paged at all, which
        is a finding in its own right.
        """
        self._check_limits("query")
        limit = min(limit or self.page_size_max, self.page_size_max)
        rows = [r for r in self._scannable() if r["last_modified"] >= since]
        rows.sort(key=lambda r: (r["last_modified"], r["id"]))
        if last_id is not None:
            rows = [r for r in rows
                    if (r["last_modified"], r["id"]) > last_id]
        page = rows[:limit]
        self.records_returned += len(page)
        return [dict(r) for r in page]

    def get_deleted(self, since, until):
        raise NotImplementedError

    def count(self):
        """A cheap count, used by the count-check reconciler."""
        self._check_limits("count")
        return len(self._live())

    def list_ids(self, offset=0, limit=None):
        """An ID-only projection. Much cheaper per record than full fetches."""
        self._check_limits("list_ids")
        limit = min(limit or 1000, 1000)
        ids = sorted(r["id"] for r in self._live())
        page = ids[offset:offset + limit]
        self.records_returned += len(page)
        return page

    def get_many(self, ids):
        """Fetch records by id, in batches the vendor permits.

        A TOMBSTONE IS NOT RETURNED. Asking a real system of either shape for a
        record it has deleted or archived does not hand back the row with a
        flag on it: the ordinary by-id read answers NOT FOUND, and the deleted
        row is reachable only by explicitly asking for deleted or archived
        rows, which is what get_deleted() and _scannable() model here.

        That absence is the only signal a by-id reader gets, and a caller that
        treats "the record I asked for is not in the response" as a deletion is
        reading the API correctly. A fake that returned the tombstone instead
        would let a connector re-upsert a deleted record as live and still look
        correct in this simulation, which is the bug this models away.
        """
        if len(ids) > self.batch_max:
            raise ValueError("batch of %d exceeds vendor max %d"
                             % (len(ids), self.batch_max))
        self._check_limits("get_many")
        out = [dict(self.records[i]) for i in ids
               if i in self.records and not self._tombstoned(self.records[i])]
        self.records_returned += len(out)
        return out

    batch_max = 100


class Atlas(Vendor):
    """Salesforce-shaped: a daily allowance, a bulk export, and real deletes."""

    name = "atlas"
    daily_allowance = 100000
    per_second_limit = 25.0
    throttled_calls_count_against_quota = True
    page_size_max = 200
    batch_max = 200
    # Deletions are queryable for a window and then become invisible. Exceed
    # the window and a full reconciliation is the ONLY way to find them.
    delete_retention_seconds = 7200.0
    # The bulk path: high latency, but one call covers a whole job.
    bulk_job_latency_seconds = 900.0
    bulk_records_per_job = 100000

    def _remove(self, rec, at):
        rec["deleted"] = True
        self.deleted_log.append((rec["id"], at))

    def _tombstoned(self, rec):
        return bool(rec["deleted"])

    def get_deleted(self, since, until):
        """The dedicated deletes endpoint, with its retention window.

        Returns only what is still retained. A caller asking about a period
        older than the retention window gets an empty answer that is
        indistinguishable from "nothing was deleted", which is the trap.
        """
        self._check_limits("get_deleted")
        now = self.clock.now()
        horizon = now - self.delete_retention_seconds
        out = [rid for rid, at in self.deleted_log
               if since <= at <= until and at >= horizon]
        self.records_returned += len(out)
        return out

    def bulk_export(self):
        """An async export job. One call, then latency, then everything.

        The point of this method is that it changes the ACCESS PATTERN rather
        than the pacing. Experiment 1 compares tuning a limiter against
        changing the access pattern, and this is the other side of that
        comparison.
        """
        self._check_limits("bulk_export")
        self.clock.advance(self.bulk_job_latency_seconds)
        self.apply_timeline_to_now()
        rows = self._live()[:self.bulk_records_per_job]
        self.records_returned += len(rows)
        return [dict(r) for r in rows]


class Beacon(Vendor):
    """HubSpot-shaped: a burst window, a search ceiling, and archive semantics."""

    name = "beacon"
    daily_allowance = 250000
    # A burst limit over a SHORT ROLLING WINDOW rather than a per-second rate.
    # The distinction matters: a burst of the full allowance is permitted, and
    # then nothing until the window rolls.
    burst_window_seconds = 10.0
    burst_limit = 100
    per_second_limit = 10.0
    throttled_calls_count_against_quota = False
    page_size_max = 100
    batch_max = 100
    # The search endpoint silently truncates beyond this many results. A scan
    # that needs to page deeper than the ceiling cannot, and the API does not
    # say so; it simply stops returning rows.
    search_result_ceiling = 10000
    # Search has its own, tighter limit than the rest of the API.
    search_per_window = 40

    def __init__(self, clock, records=None, timeline=None):
        super().__init__(clock, records, timeline)
        self._recent_search = []

    def _rate_window(self):
        return self.burst_window_seconds

    def _rate_ceiling(self):
        return self.burst_limit

    def _remove(self, rec, at):
        # Archive, not delete. The record remains readable with a flag, which
        # makes deletion detectable by an ordinary scan, if, and only if, the
        # archive operation also moves last_modified.
        rec["archived"] = True
        rec["last_modified"] = at
        self.deleted_log.append((rec["id"], at))

    def _tombstoned(self, rec):
        return bool(rec["archived"])

    def _scannable(self):
        # Archived rows remain readable. This one line is the whole difference
        # between Beacon and Atlas for delete detection, and experiment 2
        # measures what it is worth.
        return list(self.records.values())

    def search_modified_since(self, since, limit=None, offset=0):
        """The search endpoint: tighter limit, and a hard result ceiling."""
        now = self.clock.now()
        self._recent_search = [t for t in self._recent_search
                               if t > now - self.burst_window_seconds]
        if len(self._recent_search) >= self.search_per_window:
            self.calls_throttled += 1
            oldest = min(self._recent_search)
            raise RateLimited(
                retry_after=max(0.001,
                                oldest + self.burst_window_seconds - now),
                kind="search")
        self._recent_search.append(now)
        self._check_limits("search")

        limit = min(limit or self.page_size_max, self.page_size_max)
        rows = [r for r in self.records.values()
                if r["last_modified"] >= since]
        rows.sort(key=lambda r: (r["last_modified"], r["id"]))
        # The ceiling, applied silently. Everything past it is unreachable and
        # the response looks exactly like the end of the result set.
        if self.search_result_ceiling is not None:
            rows = rows[:self.search_result_ceiling]
        page = rows[offset:offset + limit]
        self.records_returned += len(page)
        return [dict(r) for r in page]

    def get_deleted(self, since, until):
        """Beacon has no deletes endpoint. Archived records come back from the
        ordinary scan instead, so archive semantics are easier to integrate
        against than hard deletes."""
        raise NotImplementedError(
            "beacon archives rather than deletes; scan for archived=true")
