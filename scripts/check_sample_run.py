"""Re-run the commands SAMPLE_RUN.md transcribes and require the SAME OUTPUT.

    python3 scripts/check_sample_run.py

SAMPLE_RUN.md says, at the top of the file, that every command was "executed
exactly as written" and that "no output below was altered". That is a
claim about the shipped tree, and this script is what checks it.

check_readme_numbers.py does this job for README.md, and the same argument
applies with more force here. A transcript looks like evidence. A reader who
would check a figure in a table will not re-run a captured session, so a stale
line in this file is believed for longer than a stale line anywhere else.

What it does not check is printed too. Blocks that cannot be compared verbatim
are named, with the reason, rather than passed over in silence: a block that was
skipped is not a block that agreed.
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lab

SAMPLE_RUN = os.path.join(lab.REPO, "SAMPLE_RUN.md")

# Commands that are deterministic to the byte: run them and compare verbatim.
VERBATIM = re.compile(r"^python3? (scripts/exp\d_\w+\.py"
                      r"|scripts/check_readme_numbers\.py)$")

# The suite prints a wall-clock duration on its last line, and that duration
# is not reproducible. Its test count is, and the count is the figure that goes
# stale, so that is what gets compared.
PYTEST = re.compile(r"^python3? -m pytest -q$")

# Anything else in the transcript. Each one needs a REASON, and the reason is
# printed, because "not checked" and "checked and agreed" must not look alike.
UNCHECKABLE = {
    "the reproducibility claim, checked":
        "a heading, not a command",
    "cp -r results /tmp/run-a && for s in scripts/exp*.py; do python3 $s "
    ">/dev/null; done && diff -r /tmp/run-a results":
        "writes outside the repository; the regenerate-and-diff step in CI "
        "makes the same assertion",
}


def blocks(text):
    """The transcript as (command, expected_output_lines) pairs."""
    fences = [i for i, line in enumerate(text.splitlines())
              if line.strip() == "```"]
    if len(fences) < 2:
        raise SystemExit("SAMPLE_RUN.md has no fenced transcript block")
    lines = text.splitlines()[fences[0] + 1:fences[-1]]
    out = []
    for line in lines:
        if line.startswith("$ "):
            out.append((line[2:], []))
        elif out:
            out[-1][1].append(line)
    return out


def trimmed(lines):
    """Drop the blank separator lines the transcript uses between blocks."""
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def main():
    with open(SAMPLE_RUN, encoding="utf-8") as fh:
        text = fh.read()

    checked = partial = 0
    skipped = []
    failures = []

    for command, expected in blocks(text):
        expected = trimmed(list(expected))

        if command in UNCHECKABLE:
            skipped.append((command, UNCHECKABLE[command]))
            continue

        if VERBATIM.match(command):
            r = subprocess.run([sys.executable] + command.split()[1:],
                               cwd=lab.REPO, capture_output=True, text=True)
            actual = trimmed(r.stdout.splitlines())
            checked += 1
            if actual != expected:
                failures.append((command, expected, actual))
            continue

        if PYTEST.match(command):
            r = subprocess.run([sys.executable, "-m", "pytest", "-q"],
                               cwd=lab.REPO, capture_output=True, text=True)
            actual = trimmed(r.stdout.splitlines())
            partial += 1
            want = [l for l in expected if " passed" in l]
            got = [l for l in actual if " passed" in l]
            strip_time = lambda ls: [re.sub(r" in [\d.]+s$", "", l) for l in ls]
            if strip_time(want) != strip_time(got):
                failures.append((command + "  (test count only; the duration "
                                 "is the one figure here that is not "
                                 "reproducible)", want, got))
            continue

        skipped.append((command, "NO RULE MATCHES THIS COMMAND -- add one"))

    print("%d transcript blocks re-run and compared verbatim, %d compared in "
          "part, %d not compared" % (checked, partial, len(skipped)))
    for command, why in skipped:
        print("  NOT COMPARED: $ %s\n      because %s" % (command, why))

    if failures:
        print()
        for command, expected, actual in failures:
            print("MISMATCH: $ %s" % command)
            for i in range(max(len(expected), len(actual))):
                e = expected[i] if i < len(expected) else "<no such line>"
                a = actual[i] if i < len(actual) else "<no such line>"
                if e != a:
                    print("  line %d" % (i + 1))
                    print("    SAMPLE_RUN.md says: %s" % e)
                    print("    the command prints: %s" % a)
        print()
        print("%d of %d compared blocks do not match what the command prints."
              % (len(failures), checked + partial))
        return 1

    # A "no rule matches" skip is a hole in this checker, not a clean result.
    unruled = [c for c, why in skipped if why.startswith("NO RULE")]
    if unruled:
        print()
        print("%d transcript command(s) have no rule in this checker."
              % len(unruled))
        return 1

    print("SAMPLE_RUN.md agrees with what the commands print")
    return 0


if __name__ == "__main__":
    sys.exit(main())
