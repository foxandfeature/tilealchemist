import threading
import time


class UpdateLineThrottle:
    """Rate-limits a worker's `update:` lines to at most one per `interval`.

    By default the first `due()` MUST wait out a full interval, so a step
    that finishes sooner stays silent (docs/ARCHITECTURE.md, "Worker
    logging"). `fire_immediately=True` makes the first call due instead, for
    curl's progress-meter behavior: instant "yes, it's downloading"
    feedback. throttle_progress.sh's show_pending_if_due() is the same
    bypass in bash.
    """

    def __init__(self, interval, fire_immediately=False):
        self.interval = interval
        self.last_fired_at = None if fire_immediately else time.monotonic()
        self.lock = threading.Lock()

    def due(self):
        with self.lock:
            now = time.monotonic()
            if self.last_fired_at is None or now - self.last_fired_at >= self.interval:
                self.last_fired_at = now
                return True
            return False
