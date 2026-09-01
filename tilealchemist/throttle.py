import threading
import time


class UpdateLineThrottle:
    """`fire_immediately=False` (the default) is what every existing caller
    wants: TransformProgress and the directory walk are meant to stay silent
    if the step finishes before the first interval elapses (see
    build_shard.py's module docstring) - `last_fired_at` starts at
    construction time, so the first `due()` still has to wait out a full
    interval like any other. `fire_immediately=True` is for callers that
    want tiledistillery's curl-progress-meter behavior instead: the very
    first call is always due, giving instant "yes, it's downloading"
    feedback (DownloadProgress's use case - see throttle_progress.sh's
    show_pending_if_due() for the same bypass on the bash side)."""

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
