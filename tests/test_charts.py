"""The ASCII bar renderer, and the two rounding decisions inside it.

A chart is a claim about shape, the one kind of claim a reader believes
without checking. The renderer is therefore pinned the same way every other
figure in this repository is: by tests that fail if the picture stops matching
the data.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

import charts                                          # noqa: E402


def bars(text):
    """The bar rows only, with the caption and its blank line dropped."""
    lines = text.split("\n")
    if lines and lines[0].startswith("bar length = "):
        lines = lines[2:]
    return lines


# ---------------------------------------------------------------------------
# THE RENDERER
# ---------------------------------------------------------------------------

def test_the_longest_bar_is_exactly_the_chart_width():
    out = charts.bar_chart([("a", 10, "10"), ("b", 5, "5")], width=20)
    assert out.split("\n")[0].count("#") == 20


def test_a_bar_is_proportional_to_its_value():
    out = charts.bar_chart([("a", 100, "100"), ("b", 50, "50")], width=40)
    first, second = [line.count("#") for line in out.split("\n")]
    assert first == 40 and second == 20


def test_a_nonzero_value_is_never_drawn_as_an_empty_bar():
    # (mutation-checked: drop the max(1, ...) and this row renders as a blank
    # line, so a reader sees nothing where the data says one call. The N+1
    # chart in the README contains exactly this case: 1 against 4,021.)
    out = charts.bar_chart([("big", 4021, "4,021"), ("small", 1, "1")],
                           width=44)
    assert out.split("\n")[1].count("#") == 1


def test_a_zero_is_drawn_as_no_bar_at_all():
    # The only case where a blank bar is allowed, which is what makes a blank
    # bar readable. Count_check detects 0.0000 and must look like it.
    out = charts.bar_chart([("a", 10, "10"), ("zero", 0, "0")], width=20)
    assert out.split("\n")[1].count("#") == 0


def test_an_all_zero_chart_does_not_divide_by_zero():
    out = charts.bar_chart([("a", 0, "0"), ("b", 0, "0")], width=20)
    assert out.count("#") == 0


def test_a_negative_value_is_refused_rather_than_drawn():
    # There is no honest bar for a negative number, and silently clamping it
    # to zero would draw a picture the data does not support.
    with pytest.raises(ValueError):
        charts.bar_chart([("a", 5, "5"), ("b", -1, "-1")], width=20)


def test_labels_are_padded_to_a_common_width_so_the_bars_line_up():
    out = charts.bar_chart([("short", 1, "1"), ("much_longer", 1, "1")],
                           width=10)
    starts = [line.index("#") for line in out.split("\n")]
    assert starts[0] == starts[1]


def test_rendering_is_a_pure_function_of_its_arguments():
    rows = [("a", 3, "3"), ("b", 7, "7")]
    assert charts.bar_chart(rows) == charts.bar_chart(rows)


def test_an_empty_chart_is_empty_rather_than_an_exception():
    assert charts.bar_chart([]) == ""


def test_one_is_singular_and_everything_else_is_plural():
    assert charts._plural(1, "call") == "1 call"
    assert charts._plural(0, "call") == "0 calls"
    assert charts._plural(4021, "call") == "4,021 calls"


# ---------------------------------------------------------------------------
# The four CHARTS the README carries
# ---------------------------------------------------------------------------

def test_every_published_chart_renders_from_the_shipped_results():
    for _, fn in charts.CHARTS:
        out = fn()
        assert out and "#" in out, fn.__name__


def test_every_published_chart_says_what_its_bars_mean():
    # An unlabeled axis is the chart failing to say what it is about. Chart 4
    # is the case that forced this: its bars are a detection rate while the
    # larger number beside them is a call count, so a reader who guesses
    # guesses wrong.
    for _, fn in charts.CHARTS:
        assert fn().startswith("bar length = "), fn.__name__


def test_the_ghost_ladder_is_five_flat_bars_and_a_cliff():
    # The central finding, asserted as a shape. If a future change makes any
    # of the first five mitigations move the ghost column, this chart stops
    # being the argument the README says it is, and a human should look
    # before the picture is published again.
    lines = bars(charts.chart_e2_ghost_records())
    assert len(lines) == 6
    widths = [line.count("#") for line in lines]
    assert len(set(widths[:5])) == 1, "the first five must be identical"
    assert widths[5] < widths[0] / 10, "the last must be a cliff, not a step"


def test_the_access_pattern_chart_keeps_the_small_bars_visible():
    # The N+1 bar is 191x the paged scan. Both smaller rows must still be
    # drawn, or the chart silently claims they cost nothing.
    for line in bars(charts.chart_e1_access_pattern_calls()):
        assert "#" in line, line


def test_the_count_check_bar_is_the_only_empty_one_in_its_chart():
    lines = bars(charts.chart_e4_detection_rate())
    empty = [line for line in lines if "#" not in line]
    assert len(empty) == 1
    assert empty[0].startswith("count check")
