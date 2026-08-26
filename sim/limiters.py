"""Client-side rate limiting strategies, against a simulated clock.

Four strategies, and the comparison is the experiment. Each decides one thing:
given that I want to make a call now, how long do I wait first?

  naive         wait for nothing. Call until throttled, then retry at once.
                Included as the baseline because it is what a connector does
                before anybody has been paged about it.
  fixed_sleep   a constant delay between calls, tuned by hand. The most common
                real implementation, and the one that is either too slow or
                too fast and never both.
  token_bucket  a bucket of B tokens refilling at R per second, with R set to
                a share of the documented limit. A standard answer.
  aimd          additive increase on success, multiplicative decrease on 429.
                The same control law as TCP congestion control, and it works
                here for the same reason: it converges on the rate the server
                will actually tolerate rather than the rate the documentation
                claims.

Every one of them advances the simulated clock rather than sleeping. A limiter
that called time.sleep would make the experiment take as long as the thing it
is measuring, and would make the result depend on this machine.
"""


class Limiter:
    """Base class. A limiter decides how long to wait before the next call."""

    name = "base"

    def __init__(self, clock):
        self.clock = clock
        self.waits = 0
        self.total_wait = 0.0

    def _wait(self, seconds):
        if seconds > 0:
            self.clock.advance(seconds)
            self.waits += 1
            self.total_wait += seconds

    def acquire(self):
        """Block (in simulated time) until a call may be made."""
        raise NotImplementedError

    def on_success(self):
        pass

    def on_throttled(self, retry_after):
        """Called after a 429. retry_after is the vendor's instruction, or None.

        HONORING Retry-After IS NOT OPTIONAL. A limiter that applies its own
        backoff and ignores an explicit instruction is choosing to be wrong
        about the one number the server actually knows.
        """
        self._wait(retry_after if retry_after else 1.0)

    def stats(self):
        return {"strategy": self.name, "waits": self.waits,
                "total_wait_seconds": round(self.total_wait, 3)}


class Naive(Limiter):
    """No pacing at all. Retries immediately on 429."""

    name = "naive"

    def acquire(self):
        return

    def on_throttled(self, retry_after):
        # The anti-pattern, modeled faithfully. It ignores Retry-After and
        # hammers. On a vendor where 429s themselves count against the quota
        # this actively accelerates exhaustion, which is exactly what the
        # experiment is meant to show.
        self.waits += 1


class FixedSleep(Limiter):
    """A constant delay between calls."""

    name = "fixed_sleep"

    def __init__(self, clock, delay_seconds):
        super().__init__(clock)
        self.delay = float(delay_seconds)
        self._last = None

    def acquire(self):
        now = self.clock.now()
        if self._last is not None:
            elapsed = now - self._last
            if elapsed < self.delay:
                self._wait(self.delay - elapsed)
        self._last = self.clock.now()


class TokenBucket(Limiter):
    """A bucket of `capacity` tokens refilling at `rate` per second.

    The rate is set below the documented limit on purpose. In a real B2B
    integration the quota belongs to the CUSTOMER and is shared with every
    other integration they have installed, so planning to consume 100% of it
    is planning to cause somebody else's outage.
    """

    name = "token_bucket"

    def __init__(self, clock, rate, capacity):
        super().__init__(clock)
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._last = clock.now()

    def _refill(self):
        now = self.clock.now()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now

    def acquire(self):
        self._refill()
        if self._tokens < 1.0:
            needed = (1.0 - self._tokens) / self.rate
            self._wait(needed)
            self._refill()
        self._tokens -= 1.0

    def stats(self):
        s = super().stats()
        s["rate"] = self.rate
        s["capacity"] = self.capacity
        return s


class AIMD(Limiter):
    """Additive increase, multiplicative decrease.

    Converges on the rate the server actually tolerates, which is not the rate
    the documentation states: the real ceiling moves with whatever else is
    consuming the customer's quota.
    """

    name = "aimd"

    def __init__(self, clock, start_rate, max_rate, increase=0.5, decrease=0.5,
                 min_rate=0.1):
        super().__init__(clock)
        self.rate = float(start_rate)
        self.max_rate = float(max_rate)
        self.min_rate = float(min_rate)
        self.increase = float(increase)
        self.decrease = float(decrease)
        self._last = clock.now()
        self.decreases = 0
        self.min_rate_seen = float(start_rate)

    def acquire(self):
        interval = 1.0 / self.rate
        now = self.clock.now()
        elapsed = now - self._last
        if elapsed < interval:
            self._wait(interval - elapsed)
        self._last = self.clock.now()

    def on_success(self):
        # ADDITIVE increase: probe for headroom slowly.
        self.rate = min(self.max_rate, self.rate + self.increase)

    def on_throttled(self, retry_after):
        # MULTIPLICATIVE decrease: give up headroom fast. The asymmetry
        # matters: it is cheap to be slightly too slow and expensive to be
        # slightly too fast.
        self.rate = max(self.min_rate, self.rate * self.decrease)
        self.decreases += 1
        self.min_rate_seen = min(self.min_rate_seen, self.rate)
        self._wait(retry_after if retry_after else 1.0)

    def stats(self):
        s = super().stats()
        s["final_rate"] = round(self.rate, 3)
        s["decreases"] = self.decreases
        s["min_rate_seen"] = round(self.min_rate_seen, 3)
        return s


def build(name, clock, documented_rate, share=0.5):
    """Construct a limiter by name, sized against a documented rate.

    `share` is the fraction of the vendor's documented limit this connector is
    willing to consume. It is a first-class parameter rather than a constant
    because it is a POLICY decision about how good a guest to be in somebody
    else's account.
    """
    target = documented_rate * share
    if name == "naive":
        return Naive(clock)
    if name == "fixed_sleep":
        return FixedSleep(clock, 1.0 / target if target > 0 else 1.0)
    if name == "token_bucket":
        return TokenBucket(clock, rate=target, capacity=max(1.0, target))
    if name == "aimd":
        return AIMD(clock, start_rate=max(0.5, target * 0.5),
                    max_rate=documented_rate)
    raise ValueError("unknown limiter strategy: %r" % (name,))


STRATEGIES = ("naive", "fixed_sleep", "token_bucket", "aimd")
