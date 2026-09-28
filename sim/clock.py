"""A simulated clock, and the reason this repository has one.

Nothing here reads the wall Clock. Every duration in every published figure is
SIMULATED SECONDS, advanced explicitly by the code under test.

WHY. A measurement that depends on how fast this machine happens to be is a
measurement that does not reproduce, and a figure that does not reproduce is
not a result. Sweeping a rate limiter against a real clock measures the
interpreter, the scheduler and the machine at least as much as it measures the
strategy. Against a simulated clock it measures the strategy.

What it buys, concretely:
  - Every number in README.md is identical on any machine, not merely close.
  - "Seconds to complete a backfill" is a property of the algorithm and the
    vendor's stated limits, which is the thing worth publishing.
  - A daily quota can be exhausted in a test that finishes in milliseconds.
  - The whole suite runs offline in under a second with no container.

What it gives up, and the README says so in its own section: this measures an
algorithm against a MODEL of a vendor. Real APIs have jitter, partial failures
and undocumented behavior. The shape of the result transfers; no claim about
wall-clock speed is made or implied.
"""


class Clock:
    """Simulated time, in seconds since the start of the run.

    Advance is explicit and monotonic. There is no way to move time backward,
    because a clock that can go backward turns every ordering bug into an
    unreproducible one.
    """

    def __init__(self, start=0.0):
        self._now = float(start)
        self.advances = 0
        self.total_advanced = 0.0

    def now(self):
        return self._now

    def advance(self, seconds):
        """Move forward. Refuses to move backward or by a NaN."""
        if seconds != seconds:                       # NaN
            raise ValueError("cannot advance by NaN")
        if seconds < 0:
            raise ValueError("cannot advance the clock backward: %r" % seconds)
        self._now += float(seconds)
        self.advances += 1
        self.total_advanced += float(seconds)
        return self._now

    def advance_to(self, when):
        """Move forward to an absolute time. A no-op if already past it."""
        if when > self._now:
            self.advance(when - self._now)
        return self._now

    def __repr__(self):
        return "Clock(now=%.3f, advances=%d)" % (self._now, self.advances)


class Skew:
    """A second clock that is WRONG relative to the harness clock.

    Models the vendor stamping last_modified from its own clock, which is not
    synchronized with yours. A skew of -11.0 means the vendor believes it is
    eleven seconds earlier than the harness does, so a record it writes "now"
    carries a timestamp already below a watermark taken from the harness clock.

    This is not a detail. It is one of the four documented ways a
    modified-since watermark silently loses records, and it is invisible in
    production because both clocks look correct from where they are standing.
    """

    def __init__(self, clock, offset_seconds=0.0):
        self.clock = clock
        self.offset = float(offset_seconds)

    def now(self):
        return self.clock.now() + self.offset
