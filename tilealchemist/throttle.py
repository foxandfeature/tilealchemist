"""Rate limiter for a worker's `update:` lines; see docs/ARCHITECTURE.md."""
import threading
import time


class UpdateLineThrottle:

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
