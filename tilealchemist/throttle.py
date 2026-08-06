import threading
import time


class UpdateLineThrottle:
    def __init__(self, interval):
        self.interval = interval
        self.last_fired_at = time.monotonic()
        self.lock = threading.Lock()

    def due(self):
        with self.lock:
            now = time.monotonic()
            if now - self.last_fired_at >= self.interval:
                self.last_fired_at = now
                return True
            return False
