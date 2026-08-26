"""The generated world and the mutation timeline that is the answer key.

Every value is a pure function of a seed and a row number. Nothing is drawn
from a global random stream and nothing reads the clock, so a clone reproduces
the same world and every number in README.md.

The timeline is the answer key. The harness knows every change that happened
and the simulated instant it occurred at, so "did the connector see this
change" is a measurement rather than an estimate. That is the same discipline
the sibling data repositories use, and the same caveat applies: the MIX of
mutation kinds is invented, so every loss rate is a function of it. What
transfers is which kinds are unfixable by which mitigation, not the rates.

The six mutation kinds, and why each one is here. Each models a documented way
that real ingestion loses data:

  UPDATE             the ordinary case. last_modified advances. A correct
                     watermark scan should catch every one of these, and if it
                     does not, the connector has a bug rather than a limit.
  SILENT_UPDATE      the record changes and last_modified DOES NOT MOVE. Models
                     a bulk import or an admin path that bypasses the trigger.
                     NO watermark scan can ever see this, at any overlap.
  IN_FLIGHT_UPDATE   committed during a sync pass, stamped before the pass
                     began. The scan has already read past that position.
  LATE_CLOCK_UPDATE  stamped from a vendor clock running behind the harness, so
                     it lands below a watermark taken from the harness clock.
  DELETE             the record is gone. There is no timestamp to scan, so a
                     watermark scan cannot see a deletion, ever.
  MERGE              two records become one. The loser is deleted (invisible)
                     and the winner is updated (visible), so a local copy keeps
                     a ghost record forever.
"""

import hashlib

SEED = "connector-sync-guarantees-2026"

# The corpus, per vendor.
N_RECORDS = 4000

# How long the mutation timeline runs, in simulated seconds. Eight hours is
# enough to contain several sync passes at any realistic interval.
TIMELINE_SECONDS = 28800

# How many mutations happen over the timeline.
N_MUTATIONS = 1200

# The mix. These are INVENTED, they are the single biggest assumption in the
# repository, and every loss rate downstream is a function of them. They are
# weighted toward ordinary updates because that is what real traffic looks
# like; the exotic kinds are rare and are exactly the ones that get missed.
MUTATION_MIX = [
    ("UPDATE", 0.60),
    ("SILENT_UPDATE", 0.08),
    ("IN_FLIGHT_UPDATE", 0.10),
    ("LATE_CLOCK_UPDATE", 0.07),
    ("DELETE", 0.11),
    ("MERGE", 0.04),
]

# How far behind the harness clock the vendor's clock runs, in seconds, for a
# LATE_CLOCK_UPDATE. Real skew between independent systems is routinely single
# -digit seconds and occasionally much worse.
VENDOR_CLOCK_SKEW = -11.0

FIELDS = ("name", "owner", "stage", "amount", "region")
STAGES = ("new", "qualified", "proposal", "negotiation", "closed")
REGIONS = ("amer", "emea", "apac")
OWNERS = ("alice", "bob", "carmen", "dev", "erin", "frank", "gita", "hal")


# ---------------------------------------------------------------------------
# THE DETERMINISTIC STREAM
# ---------------------------------------------------------------------------

def _bits(*parts):
    """64 bits keyed by SEED and by every part of the coordinate.

    Keyed rather than concatenated so that ("a", "bc") and ("ab", "c") cannot
    collide, which is the failure that makes a generator look random while
    quietly correlating two fields.
    """
    msg = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(
        hashlib.blake2b(msg, digest_size=8, key=SEED.encode()).digest(), "big")


def unit(*parts):
    return _bits(*parts) / 2.0 ** 64


def pick(seq, *parts):
    return seq[_bits(*parts) % len(seq)]


def weighted(mix, *parts):
    """Choose from [(value, weight), ...] deterministically."""
    total = sum(w for _, w in mix)
    x = unit(*parts) * total
    acc = 0.0
    for value, w in mix:
        acc += w
        if x < acc:
            return value
    return mix[-1][0]


# ---------------------------------------------------------------------------
# RECORDS
# ---------------------------------------------------------------------------

def initial_record(vendor, i):
    """One record as it exists before the timeline starts.

    last_modified is spread over NEGATIVE time, before the run begins, so that
    a backfill has a realistic distribution to page through and the first
    incremental watermark has something to sit above.
    """
    return {
        "id": "%s-%06d" % (vendor[0].upper(), i),
        "name": "Record %06d" % i,
        "owner": pick(OWNERS, vendor, "owner", i),
        "stage": pick(STAGES, vendor, "stage", i),
        "amount": 1000 + _bits(vendor, "amount", i) % 99000,
        "region": pick(REGIONS, vendor, "region", i),
        "last_modified": -float(_bits(vendor, "lm", i) % 864000),
        "deleted": False,
        "archived": False,
        "merged_into": None,
        "version": 1,
    }


def initial_world(vendor, n=N_RECORDS):
    return {r["id"]: r for r in (initial_record(vendor, i)
                                 for i in range(1, n + 1))}


# ---------------------------------------------------------------------------
# The mutation timeline: the answer key
# ---------------------------------------------------------------------------

def mutation_timeline(vendor, n_records=N_RECORDS, n_mutations=N_MUTATIONS):
    """Every change that happens, in simulated-time order.

    Returns a list of dicts, each with:
        seq            position in the timeline
        at             simulated seconds when it happens
        kind           one of the six kinds
        record_id      the record it affects
        stamp          the last_modified value the vendor will store, which is
                       NOT always `at`, and the discrepancy is what the
                       simulation exists to produce
        field, value   what actually changed (None for DELETE)
        merged_into    for MERGE, the surviving record

    THE `stamp` field is where the losses come from. For an UPDATE it equals
    `at`. For SILENT_UPDATE it keeps the previous value, for LATE_CLOCK_UPDATE
    it is `at` plus a negative skew, and for IN_FLIGHT_UPDATE it is backdated
    into the pass that is currently running. A connector filtering on the
    stamp cannot distinguish these from records it has already seen.
    """
    # the timeline is built in time order, and it has to be. The `stamp` a
    # SILENT_UPDATE preserves is the record's CURRENT stamp, which is only
    # meaningful if every earlier mutation has already been applied. Drawing
    # the mutations in sequence order and sorting by time afterwards lets a
    # silent update copy a stamp set by a mutation that has not happened yet,
    # which puts a timestamp in the FUTURE, and a watermark that lands there
    # silently disables incremental sync for the rest of the run while still
    # producing a plausible accuracy figure.
    draws = []
    for s in range(1, n_mutations + 1):
        draws.append((TIMELINE_SECONDS * unit(vendor, "mut-at", s), s))
    draws.sort()

    out = []
    stamps = {}
    for i in range(1, n_records + 1):
        rec = initial_record(vendor, i)
        stamps[rec["id"]] = rec["last_modified"]

    deleted = set()
    for at, s in draws:
        kind = weighted(MUTATION_MIX, vendor, "mut-kind", s)

        # Choose a live record. Skewed toward low ids so repeated mutations of
        # the same record are common, which is what makes out-of-order
        # delivery and stale overwrites possible.
        idx = 1 + int(n_records ** unit(vendor, "mut-rec", s)) % n_records
        rid = "%s-%06d" % (vendor[0].upper(), idx)
        if rid in deleted:
            kind = "UPDATE"                       # cannot mutate a dead record
            rid = "%s-%06d" % (vendor[0].upper(),
                               1 + (idx * 7 + s) % n_records)
            if rid in deleted:
                continue

        entry = {"seq": s, "at": at, "kind": kind, "record_id": rid,
                 "field": None, "value": None, "merged_into": None}

        if kind == "DELETE":
            entry["stamp"] = stamps[rid]          # no new stamp; it is gone
            deleted.add(rid)
        elif kind == "MERGE":
            other = "%s-%06d" % (vendor[0].upper(),
                                 1 + (idx * 13 + s) % n_records)
            if other == rid or other in deleted:
                # Degenerate merge; treat as an ordinary update rather than
                # inventing a self-merge no real system produces.
                entry["kind"] = kind = "UPDATE"
            else:
                entry["merged_into"] = other
                entry["stamp"] = stamps[rid]
                deleted.add(rid)
                # The survivor is genuinely modified by absorbing the loser.
                stamps[other] = at
        if kind in ("UPDATE", "SILENT_UPDATE", "IN_FLIGHT_UPDATE",
                    "LATE_CLOCK_UPDATE"):
            field = pick(FIELDS, vendor, "mut-field", s)
            entry["field"] = field
            if field == "amount":
                entry["value"] = 1000 + _bits(vendor, "mut-val", s) % 99000
            elif field == "stage":
                entry["value"] = pick(STAGES, vendor, "mut-val", s)
            elif field == "owner":
                entry["value"] = pick(OWNERS, vendor, "mut-val", s)
            elif field == "region":
                entry["value"] = pick(REGIONS, vendor, "mut-val", s)
            else:
                entry["value"] = "Record %06d rev %d" % (idx, s)

            if kind == "UPDATE":
                entry["stamp"] = at
            elif kind == "SILENT_UPDATE":
                # The unfixable one. The stamp does not move at all.
                entry["stamp"] = stamps[rid]
            elif kind == "LATE_CLOCK_UPDATE":
                entry["stamp"] = at + VENDOR_CLOCK_SKEW
            else:                                  # IN_FLIGHT_UPDATE
                # Backdated by up to two minutes: committed now, stamped as
                # though it occurred before the running pass started.
                entry["stamp"] = at - 120.0 * unit(vendor, "mut-back", s)

            if entry["stamp"] > stamps[rid]:
                stamps[rid] = entry["stamp"]

        out.append(entry)

    out.sort(key=lambda e: (e["at"], e["seq"]))
    for i, e in enumerate(out, start=1):
        e["seq"] = i
    return out


def manifest(vendors=("atlas", "beacon")):
    """A hash over the generated world, so a run can prove it used the same
    input as the run that produced the shipped results."""
    h = hashlib.sha256()
    counts = {}
    for v in vendors:
        world = initial_world(v)
        for rid in sorted(world):
            r = world[rid]
            h.update(("|".join(str(r[k]) for k in sorted(r))).encode())
            h.update(b"\n")
        tl = mutation_timeline(v)
        for e in tl:
            h.update(("|".join(str(e[k]) for k in sorted(e))).encode())
            h.update(b"\n")
        by_kind = {}
        for e in tl:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        counts[v] = {"records": len(world), "mutations": len(tl),
                     "by_kind": by_kind}
    return {"seed": SEED, "vendors": counts,
            "timeline_seconds": TIMELINE_SECONDS,
            "vendor_clock_skew_seconds": VENDOR_CLOCK_SKEW,
            "sha256": h.hexdigest()}
