import threading
import time


class UpdateLineThrottle:
    """Rate-limits a worker's `update:` lines to at most one per `interval`.

    By default the first `due()` has to wait out a full interval like any
    other: TransformProgress and the pmtiles directory walk are meant to
    stay silent when their step finishes before that (see
    docs/ARCHITECTURE.md's "Worker logging"). `fire_immediately=True`
    instead makes the very first call due, for curl's progress-meter
    behavior: instant "yes, it's downloading" feedback, which is what
    DownloadProgress wants (throttle_progress.sh's show_pending_if_due()
    is the same bypass on the bash side).
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
