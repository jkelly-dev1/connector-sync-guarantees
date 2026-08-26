"""ASCII bar charts, rendered from results/*.json and nothing else.

    python3 scripts/charts.py          # print every chart the README carries

Why there are CHARTS at all, and why they are ASCII. Three of the findings in
this repository are about the SHAPE of a column rather than any single number
in it: most of all the ghost- record ladder, where five mitigations in a row
change nothing and the sixth changes everything. A table states that; a bar
makes it impossible to miss.

They are ASCII because the README's headline claim is that the whole
dependency list is `python3`. A plotting library would make that false, and it
is the claim that makes this repository cheap for a stranger to actually run.

They are derived, not drawn. Every bar below is computed from the shipped
results files, and scripts/check_readme_numbers.py requires each rendered block
VERBATIM in README.md. A chart that has drifted from the data fails the same
check a stale number fails. Hand-drawn ASCII art would have been faster to
write and would have started lying the first time a result changed.

Two rendering decisions that are honesty decisions, not taste:

  A nonzero value is never drawn as an empty bar. Scaled linearly against
  4,021, a value of 1 rounds to zero characters, and a reader would see a blank
  line where the data says "one call". Any nonzero value gets at least one
  character. The bars are therefore not perfectly proportional at the very
  bottom of the range, and the numeric label is what carries the actual value.

  A zero is drawn as nothing at all. `count_check` detects 0.0000 of the
  drift, and an empty bar is the correct picture of that. This is the only
  case where a blank bar is allowed, which is what makes it readable.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lab

WIDTH = 44          # characters in the longest bar
FILL = "#"


def _plural(count, singular, plural=None):
    """"1 call" and "21 calls". A chart that says "1 calls" reads as generated
    text and invites the reader to stop trusting the rest of it."""
    count = int(count)
    word = singular if count == 1 else (plural or singular + "s")
    return "{:,} {}".format(count, word)


def bar_chart(rows, caption=None, width=WIDTH, fill=FILL):
    """Render (label, value, annotation) rows as a proportional ASCII chart.

    Caption says what bar length means, and every published chart sets it. A
    bar next to a four-column table is ambiguous by default: the reader has
    to guess which column it encodes, and in chart 4 they would guess wrong,
    because the bar is the detection rate while the more eye-catching number
    beside it is a call count. An unlabeled axis is not a minor omission in a
    chart; it is the chart failing to say what it is about.

    A pure function of its arguments: no clock, no randomness, no results
    lookup. That is what lets the checker rebuild a chart and compare it
    verbatim, and what lets the tests pin the two rounding decisions above.
    """
    rows = list(rows)
    if not rows:
        return ""
    values = [float(v) for _, v, _ in rows]
    if min(values) < 0:
        raise ValueError("a negative value has no honest bar: %r" % values)
    top = max(values)
    label_w = max(len(str(l)) for l, _, _ in rows)

    out = ["bar length = %s" % caption, ""] if caption else []
    for label, value, annotation in rows:
        value = float(value)
        if top == 0 or value == 0:
            n = 0
        else:
            # At least one character for anything nonzero. See the module
            # docstring: this is deliberate and it is why the numeric
            # annotation, not the bar, is the authoritative value.
            n = max(1, int(round(width * value / top)))
        out.append("%-*s  %-*s  %s"
                   % (label_w, label, width, fill * n, annotation))
    return "\n".join(line.rstrip() for line in out)


# ---------------------------------------------------------------------------
# THE CHARTS THE README CARRIES. Each one returns a block of text that
# check_readme_numbers.py requires verbatim, so this list and the README
# cannot disagree without the check going red.
# ---------------------------------------------------------------------------

def chart_e1_rate_bound_calls():
    """Calls issued to do 21 calls of work, under a per-second ceiling."""
    e1 = lab.read_result("exp1_rate_limits")
    rows = []
    for r in e1["scenarios"]["rate_bound"]["runs"]:
        note = _plural(r["calls"], "call")
        if r["calls_throttled"]:
            note += "  (%s rejected)" % "{:,}".format(int(r["calls_throttled"]))
        rows.append((r["strategy"], r["calls"], note))
    return bar_chart(rows, "calls issued, including rejected ones")


def chart_e1_access_pattern_calls():
    """The comparison that dwarfs the limiter sweep."""
    e1 = lab.read_result("exp1_rate_limits")
    label_of = {"paged_scan": "paged scan",
                "paged_scan_plus_detail_fetch": "paged scan + detail fetch (N+1)",
                "bulk_export": "async bulk export"}
    rows = [(label_of[r["pattern"]], r["calls"], _plural(r["calls"], "call"))
            for r in e1["access_patterns"]["rate_bound"]]
    return bar_chart(rows, "calls issued -- NOT simulated seconds")


def chart_e2_ghost_records():
    """The point of the whole repository, as a shape. Five mitigations move
    this column by nothing and the sixth moves it by 103."""
    e2 = lab.read_result("exp2_incremental_loss")
    label_of = {"naive": "naive", "+tiebreaker": "+ tiebreaker",
                "+inclusive_bound": "+ inclusive bound",
                "+overlap_60s": "+ overlap 60s",
                "+overlap_300s": "+ overlap 300s",
                "+deletes_api": "+ deletes endpoint"}
    rows = []
    for r in e2["vendors"]["atlas"]["ladder"]:
        if r["label"] not in label_of:
            continue
        g = int(r["score"]["ghost_records"])
        rows.append((label_of[r["label"]], g,
                     _plural(g, "ghost record")))
    return bar_chart(rows, "ghost records still present")


def chart_e4_detection_rate():
    """What each reconciliation strategy actually catches."""
    e4 = lab.read_result("exp4_reconciliation")
    label_of = {"count_check": "count check",
                "full_id_inventory": "full id inventory",
                "partitioned_checksum": "partitioned checksum",
                "full_field_compare": "full field compare"}
    rows_by_name = {r["strategy"]: r for r in e4["vendors"]["atlas"]}
    rows = []
    for name in ("count_check", "full_id_inventory", "partitioned_checksum",
                 "full_field_compare"):
        r = rows_by_name[name]
        rate = float(r["detection_rate"])
        rows.append((label_of[name], rate,
                     "%.4f  (%s)" % (rate, _plural(r["calls"], "call"))))
    return bar_chart(rows, "detection rate -- NOT the call count in brackets")


CHARTS = [
    ("1. calls issued to do 21 calls of work (rate-bound)",
     chart_e1_rate_bound_calls),
    ("2. calls by access pattern", chart_e1_access_pattern_calls),
    ("3. ghost records down the mitigation ladder", chart_e2_ghost_records),
    ("4. reconciliation detection rate", chart_e4_detection_rate),
]


def main():
    for title, fn in CHARTS:
        print(title)
        print()
        print(fn())
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
