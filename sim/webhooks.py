"""A webhook channel with the properties real ones actually have.

Webhooks are modeled as lossy, not as a message bus. Every property below is
something vendors document, or fail to document and do anyway:

  AT-LEAST-ONCE      duplicates arrive. Idempotent processing is not optional.
  LOSSY              a fraction of deliveries never arrive at all. There is
                     usually no way to know which.
  OUT OF ORDER       event 2 can arrive before event 1, which means a naive
                     handler can overwrite a new value with an old one.
  DELAYED            delivery latency is not zero and is not constant.
  DISABLED ON FAILURE   after sustained delivery failures many vendors stop
                     sending entirely. The receiver looks healthy and the
                     events simply stop, which is the failure worth measuring
                     because it is invisible from the inside.

The event is a hint, not data. A connector treats a delivery as "something
about record X changed" and re-reads the record. Trusting the payload would
add an ordering bug on top of a delivery bug.
"""

from sim import world as W


class WebhookChannel:
    """Emits deliveries for mutations, with realistic defects."""

    def __init__(self, vendor, loss_rate=0.03, duplicate_rate=0.05,
                 reorder_rate=0.04, base_delay=2.0, jitter=8.0,
                 disable_after_consecutive_failures=25):
        self.vendor = vendor
        self.loss_rate = loss_rate
        self.duplicate_rate = duplicate_rate
        self.reorder_rate = reorder_rate
        self.base_delay = base_delay
        self.jitter = jitter
        self.disable_after = disable_after_consecutive_failures

        self.subscription_active = True
        self.consecutive_failures = 0
        self.emitted = 0
        self.dropped_by_loss = 0
        self.dropped_by_disabled = 0
        self.duplicated = 0
        self.reordered = 0
        self.disabled_at = None

    def deliveries_for(self, timeline, receiver_down_from=None,
                       receiver_down_until=None):
        """Build the delivery stream for a whole timeline.

        Returns a list of (arrival_time, event) sorted by arrival, where an
        event is {event_id, record_id, at, seq}. A delivery that arrives while
        the receiver is down counts as a FAILURE, and enough consecutive
        failures disable the subscription permanently: after which nothing
        arrives even once the receiver recovers.
        """
        out = []
        for m in timeline:
            if not self.subscription_active:
                self.dropped_by_disabled += 1
                continue

            self.emitted += 1
            delay = self.base_delay + self.jitter * W.unit(
                self.vendor.name, "wh-delay", m["seq"])
            arrival = m["at"] + delay

            down = (receiver_down_from is not None
                    and receiver_down_from <= arrival <= receiver_down_until)
            if down:
                # The subscription dies silently. Consecutive failed deliveries
                # trip the vendor's own disable threshold, and no further event
                # is ever sent; including after the receiver comes back.
                self.consecutive_failures += 1
                if self.consecutive_failures >= self.disable_after:
                    self.subscription_active = False
                    self.disabled_at = arrival
                continue
            self.consecutive_failures = 0

            if W.unit(self.vendor.name, "wh-loss", m["seq"]) < self.loss_rate:
                self.dropped_by_loss += 1
                continue

            event = {"event_id": "%s-evt-%06d" % (self.vendor.name, m["seq"]),
                     "record_id": m["record_id"], "at": m["at"],
                     "seq": m["seq"]}

            if W.unit(self.vendor.name, "wh-reorder", m["seq"]) < self.reorder_rate:
                # Push it back behind whatever comes next.
                arrival += 30.0
                self.reordered += 1
            out.append((arrival, dict(event)))

            if W.unit(self.vendor.name, "wh-dup", m["seq"]) < self.duplicate_rate:
                self.duplicated += 1
                out.append((arrival + 1.0, dict(event)))

        out.sort(key=lambda x: x[0])
        return out

    def stats(self):
        return {
            "emitted": self.emitted,
            "dropped_by_loss": self.dropped_by_loss,
            "dropped_after_subscription_disabled": self.dropped_by_disabled,
            "duplicated": self.duplicated,
            "reordered": self.reordered,
            "subscription_active_at_end": self.subscription_active,
            "disabled_at": (round(self.disabled_at, 1)
                            if self.disabled_at is not None else None),
        }
