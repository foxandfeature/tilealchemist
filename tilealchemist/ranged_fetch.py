"""HTTP Range fetching against the source PMTiles archive, shared by the
only two things that ever talk to it: `prepare_shards.py` (one request for
the header, one for the whole directory index) and `build_shard.py` (one
request for a worker's whole batch of tile data).

Both used to carry their own near-identical copy of this - same retry
constants, same `make_session()`, same 206-vs-200 check, same
warning-line format - differing only in read timeout, in what their
progress line says, and in whether they retried a mid-stream connection
drop. Those are parameters, not separate implementations, so they're
parameters here: `timeout`, `DownloadProgress(label=...)`, and (now
unconditionally, since a dropped index download deserves a retry exactly
as much as a dropped tile-data download) the stream-error retry.
"""
import sys
import threading
import time

import requests

from tilealchemist.backoff import backoff_delay
from tilealchemist.throttle import UpdateLineThrottle

# Retries cover the transient ways the CDN fails under many concurrent
# workers hitting a freshly-published archive at once (cold-cache
# stampede): a full 200 instead of a 206 (server ignored the Range header,
# and reading the response in full would be tens of GB), a 429/5xx
# (rate-limiting or buckling under the burst), or the connection dropping
# mid-stream on a large transfer. None is a permanent failure, so all are
# worth a few backed-off retries before giving up.
MAX_RANGE_ATTEMPTS = 6
RANGE_RETRY_BASE_DELAY = 2.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Read timeouts differ by call site rather than being one shared constant:
# the directory index is a bounded, quick transfer, while a worker's batch
# is tens to low hundreds of MB and legitimately takes far longer between
# useful reads.
INDEX_READ_TIMEOUT = 60
TILE_DATA_READ_TIMEOUT = 300


def make_session():
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    session.mount("https://", adapter)
    return session


class DownloadProgress:
    """Throttled `update: ...` progress lines for one ranged download.

    `label` names what is being fetched ("directory index", "tile data").
    `add()`'s optional `tile` is build_shard.py's rough "which tile are we
    around right now" estimate; prepare_shards.py has no tile mapping to
    report for the index download and simply omits it.

    The lock is only load-bearing for build_shard.py, whose transform phase
    can report from several threads; prepare_shards.py fetches on a single
    thread and just pays an uncontended lock check.
    """

    def __init__(self, total_bytes, interval, label):
        self.total_bytes = total_bytes
        self.label = label
        self.downloaded = 0
        self.throttle = UpdateLineThrottle(interval, fire_immediately=True)
        self.lock = threading.Lock()

    def add(self, byte_count, tile=None):
        with self.lock:
            self.downloaded += byte_count
            downloaded = self.downloaded
        if not self.throttle.due():
            return
        percent_complete = (100 * downloaded / self.total_bytes) if self.total_bytes else 100.0
        location = ""
        if tile is not None:
            zoom, tile_column, tile_row = tile
            location = f", around tile z{zoom}/x{tile_column}/y{tile_row}"
        print(f"update: downloading {self.label}: "
              f"{downloaded}/{self.total_bytes} bytes ({percent_complete:.1f}%){location}",
              file=sys.stderr)


def _warn_and_wait(retry_label, attempt, delay, detail):
    """Prints the retry directly as a `::warning::` workflow command (rather
    than a plain line now and a replayed `::warning::` later) so a run that
    hit the OpenFreeMap CDN's cold-cache stampede shows up as an actual
    warning status in the Actions UI as it happens, with no duplicate line.
    Safe to call unprotected from concurrent callers: CPython serializes the
    underlying print()."""
    print(f"::warning title={retry_label} retry::{detail} "
          f"(attempt {attempt}/{MAX_RANGE_ATTEMPTS}), retrying in {delay:.0f}s",
          file=sys.stderr)
    time.sleep(delay)


def _attempt_fetch_range(session, url, range_header, retry_label, timeout, attempt,
                          on_chunk, chunk_size):
    """One attempt at fetching `range_header`. Returns the response body on
    success, or None once the backoff for a retryable failure has already
    been waited out and the caller should try again. Raises on the final
    attempt, and on any non-retryable failure."""
    with session.get(url, headers={"Range": range_header}, timeout=timeout,
                     stream=True) as response:
        if response.status_code in RETRYABLE_STATUS_CODES:
            if attempt == MAX_RANGE_ATTEMPTS:
                response.raise_for_status()
            delay = backoff_delay(attempt, response, RANGE_RETRY_BASE_DELAY)
            response.close()
            _warn_and_wait(retry_label, attempt, delay,
                           f"got HTTP {response.status_code} for range {range_header}")
            return None

        response.raise_for_status()
        if response.status_code != 206:
            if attempt == MAX_RANGE_ATTEMPTS:
                raise RuntimeError(
                    f"expected HTTP 206 Partial Content for ranged request ({range_header}) "
                    f"after {MAX_RANGE_ATTEMPTS} attempts, got {response.status_code}: server "
                    f"ignored the Range header and would send the entire archive instead of "
                    f"just this range"
                )
            delay = backoff_delay(attempt, response, RANGE_RETRY_BASE_DELAY)
            response.close()
            _warn_and_wait(retry_label, attempt, delay,
                           f"got HTTP {response.status_code} instead of 206 "
                           f"for range {range_header}")
            return None

        chunks = []
        downloaded = 0
        try:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                chunks.append(chunk)
                downloaded += len(chunk)
                if on_chunk is not None:
                    on_chunk(len(chunk), downloaded)
            return b"".join(chunks)
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError) as error:
            # Seen as an IncompleteRead well past the halfway point on a
            # large batch. No response left to read a Retry-After from (the
            # connection already broke), so back off on jitter alone.
            if attempt == MAX_RANGE_ATTEMPTS:
                raise
            delay = backoff_delay(attempt, None, RANGE_RETRY_BASE_DELAY)
            _warn_and_wait(retry_label, attempt, delay,
                           f"connection dropped after {downloaded} bytes "
                           f"({error.__class__.__name__})")
            return None


def fetch_range(session, url, offset, length, retry_label, timeout,
                on_chunk=None, chunk_size=1024 * 1024):
    """`length` bytes of `url` starting at `offset`, retried per
    MAX_RANGE_ATTEMPTS.

    `on_chunk(chunk_length, downloaded_so_far)` is called per received
    chunk, where `downloaded_so_far` counts bytes within the *current*
    attempt (so a caller mapping bytes onto positions in the requested
    range stays correct after a mid-stream retry restarts the transfer).
    `retry_label` names this call site in retry warnings.
    """
    range_header = f"bytes={offset}-{offset + length - 1}"
    for attempt in range(1, MAX_RANGE_ATTEMPTS + 1):
        body = _attempt_fetch_range(session, url, range_header, retry_label, timeout,
                                     attempt, on_chunk, chunk_size)
        if body is not None:
            return body
    # Unreachable: the final attempt either returns a body or raises above.
    raise RuntimeError(f"ranged fetch of {range_header} exhausted "
                       f"{MAX_RANGE_ATTEMPTS} attempts without a result")
