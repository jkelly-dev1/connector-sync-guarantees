"""The README's "Claims backed by tests" table, checked against the suite.

Every row of that table cites `tests/<file>.py::<name>`. A renamed or deleted
test would leave the README pointing at nothing while still telling a reader
the claim beside it was backed, which is worse than an unbacked claim stated
plainly: the citation is what stops the reader checking.

This is not a completeness check. The table is a curated subset of the suite,
and a rule that every test must appear would turn it into a changelog. What has to hold is that every row it does
carry names something that exists and runs.
"""

import ast
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(REPO, "tests")
CITATION = re.compile(r"`(tests/[\w./-]+\.py)::(\w+)`")


def _readme_citations():
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
        return CITATION.findall(fh.read())


def _defined_test_names(rel_path):
    """Top-level test function names in one file, by parsing it.

    Parsed rather than grepped, so that a name inside a docstring or a comment,
    which is exactly what a stale citation looks like once the function is
    gone, cannot satisfy the check.
    """
    path = os.path.join(REPO, rel_path)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    return {node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")}


def test_every_test_the_readme_names_actually_exists():
    # (mutation-checked: rename any cited test, or point one row at a file
    # that does not exist, and this names the row)
    citations = _readme_citations()
    # A regex that stopped matching would make this test green over an empty
    # list, which is the shape of check that reports clean because it looked at
    # nothing.
    assert len(citations) >= 50, len(citations)

    by_file = {}
    for rel_path, name in citations:
        by_file.setdefault(rel_path, []).append(name)

    broken = []
    for rel_path, names in sorted(by_file.items()):
        defined = _defined_test_names(rel_path)
        if defined is None:
            broken += ["%s::%s (no such file)" % (rel_path, n) for n in names]
            continue
        broken += ["%s::%s" % (rel_path, n) for n in names if n not in defined]
    assert broken == [], broken


# The CI workflow explains WHY it regenerates the results before trusting the
# suite, and quotes how many tests would otherwise be reading a stale file. That
# is a derived figure living in a comment, which is the one place nothing ever
# rechecks it. The count in the comment and the count in the suite are taken
# from the same definition, stated here: a test function that takes one of the
# results fixtures, or reaches for one through request.getfixturevalue.
CI = os.path.join(REPO, ".github", "workflows", "ci.yml")
RESULT_FIXTURES = {"exp1", "exp2", "exp3", "exp4"}


def _result_backed_test_functions():
    names = []
    for entry in sorted(os.listdir(TESTS)):
        if not (entry.startswith("test_") and entry.endswith(".py")):
            continue
        path = os.path.join(TESTS, entry)
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=path)
        for node in tree.body:
            if not (isinstance(node, ast.FunctionDef)
                    and node.name.startswith("test_")):
                continue
            args = {a.arg for a in node.args.args}
            body = ast.get_source_segment(source, node) or ""
            if args & RESULT_FIXTURES or "getfixturevalue" in body:
                names.append("%s::%s" % (entry, node.name))
    return names


def test_the_ci_comment_counts_the_result_backed_tests_correctly():
    # (mutation-checked: change the number in ci.yml, or add a test that takes
    # an exp fixture without updating it, and this fails)
    #
    # Never hand-type a derived figure. This one had already drifted once.
    backed = _result_backed_test_functions()
    assert len(backed) > 20, backed
    with open(CI, encoding="utf-8") as fh:
        ci = fh.read()
    claimed = re.search(r"(\d+) test functions assert over results/\*\.json",
                        ci)
    assert claimed is not None, \
        "ci.yml no longer states how many tests read results/*.json"
    assert int(claimed.group(1)) == len(backed), \
        "ci.yml says %s, the suite has %d: %s" % (claimed.group(1),
                                                  len(backed), backed)



# The README states some figures more than once, and states others in words
# ("fifteen times", "three quarters", "the 101st call"). The figures checker
# compares by substring, so one derived copy satisfies it and every other copy
# is unowned. These rows each derive one such sentence; each must exist and
# must match exactly one place in the README, or it is not anchoring anything.
ANCHORED_ROWS = (
    "world size, section 1",
    "world size, access patterns",
    "world size, section 4",
    "e2 wrong records heading",
    "world timeline hours",
    "e1 rate ceiling",
    "e1 refused call",
    "e1 fastest",
    "e1 cap share wasted",
    "e2 overlap span",
    "e4 id pages",
    "prediction tally",
)


def _figure_rows():
    import importlib.util
    path = os.path.join(REPO, "scripts", "check_readme_numbers.py")
    spec = importlib.util.spec_from_file_location("check_readme_numbers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.build())


def test_every_restated_readme_figure_has_its_own_derived_row():
    # (mutation-checked: drop a row from build(), or anchor one on words the
    # README does not carry, and this names it)
    rows = _figure_rows()
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
        flat = re.sub(r"\s+", " ", fh.read())
    absent = [label for label in ANCHORED_ROWS if label not in rows]
    assert absent == [], "no such row in check_readme_numbers.py: %s" % absent
    not_once = ["%s: %r found %d times" % (label, rows[label],
                                           flat.count(re.sub(r"\s+", " ",
                                                             rows[label])))
                for label in ANCHORED_ROWS
                if flat.count(re.sub(r"\s+", " ", rows[label])) != 1]
    assert not_once == [], not_once
