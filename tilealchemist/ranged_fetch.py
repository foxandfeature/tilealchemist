"""HTTP Range fetching against the source PMTiles archive, shared by the
only two things that ever talk to it: `pmtiles_index.py` (one request for
the header, one for the whole directory index, both on the prepare-shards
side) and `fetch_batching.py` (one request for a worker's whole batch of
tile data).

The two call sites differ only in what their progress line says and in
how retry warnings name them, so those are parameters:
`DownloadProgress(label=...)` and `retry_label`.
"""
import sys
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

READ_TIMEOUT = (10, 60)


def make_session():
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    session.mount("https://", adapter)
    return session


class DownloadProgress:
    """Throttled `update: ...` progress lines for one ranged download.

    `label` names what is being fetched ("directory index", "tile data").
    Both call sites feed this from the single `iter_content` loop below, so
    nothing here needs to be thread-safe.

    `update` takes the bytes received so far in the current attempt, not a
    delta, so a retry that restarts the transfer rewinds the line instead of
    counting the re-sent bytes a second time and running past 100%.
    """

    def __init__(self, total_bytes, interval, label):
        self.total_bytes = total_bytes
        self.label = label
        self.throttle = UpdateLineThrottle(interval, fire_immediately=True)

    def update(self, downloaded):
        if not self.throttle.due():
            return
        percent = (100 * downloaded / self.total_bytes) if self.total_bytes else 100.0
        print(f"update: downloading {self.label}: "
              f"{downloaded}/{self.total_bytes} bytes ({percent:.1f}%)",
              file=sys.stderr)


class _RetryableFailure(Exception):
    """One attempt failed in a way worth another try. `detail` names it in the
    retry warning, `response` carries a Retry-After header (None when the
    connection broke and there is no response left to read one from), and
    `final` is raised in its place once the attempts run out."""

    def __init__(self, detail, response, final):
        super().__init__(detail)
        self.detail = detail
        self.response = response
        self.final = final


def _attempt_fetch_range(session, url, range_header, on_chunk, chunk_size):
    """The response body for `range_header`, or `_RetryableFailure` for the
    transient ways this fails. Leaving the `with` while that propagates closes
    the response, so the connection is back in the pool before the caller's
    backoff sleeps on it."""
    with session.get(url, headers={"Range": range_header}, timeout=READ_TIMEOUT,
                     stream=True) as response:
        status = response.status_code

        if status in RETRYABLE_STATUS_CODES:
            raise _RetryableFailure(
                f"got HTTP {status} for range {range_header}", response,
                requests.HTTPError(f"HTTP {status} ({response.reason}) for range "
                                   f"{range_header} of {url}", response=response))

        response.raise_for_status()
        if status != 206:
            raise _RetryableFailure(
                f"got HTTP {status} instead of 206 for range {range_header}", response,
                RuntimeError(
                    f"expected HTTP 206 Partial Content for ranged request ({range_header}) "
                    f"after {MAX_RANGE_ATTEMPTS} attempts, got {status}: server ignored the "
                    f"Range header and would send the entire "
                    f"archive instead of just this range"))

        chunks = []
        downloaded = 0
        try:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                chunks.append(chunk)
                downloaded += len(chunk)
                if on_chunk is not None:
                    on_chunk(downloaded)
            return b"".join(chunks)
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError) as error:
            # Seen as an IncompleteRead well past the halfway point on a
            # large batch. The connection already broke, so there is no
            # Retry-After to honour and the backoff runs on jitter alone.
            raise _RetryableFailure(
                f"connection dropped after {downloaded} bytes "
                f"({error.__class__.__name__})", None, error) from error


def _warn_retry(retry_label, detail, attempt, delay):
    """A workflow command emitted as the retry happens, so a cold-cache
    stampede surfaces in the Actions UI instead of only in the job log.
    `retry_label` names the call site that hit it."""
    print(f"::warning title={retry_label} retry::{detail} "
          f"(attempt {attempt}/{MAX_RANGE_ATTEMPTS}), retrying in {delay:.0f}s",
          file=sys.stderr)


def fetch_range(session, url, offset, length, retry_label,
                on_chunk=None, chunk_size=1024 * 1024):
    """`length` bytes of `url` starting at `offset`, retried per
    MAX_RANGE_ATTEMPTS. Owning the loop, this also owns when to stop: the
    last attempt re-raises the underlying failure instead of backing off.

    `on_chunk(bytes_so_far)` is called per received chunk with the running
    total for the current attempt. A retry restarts the transfer, so that
    total restarts at zero too -- the count tracks what has actually
    arrived rather than growing past the requested length.
    `retry_label` names this call site in the retry warnings.
    """
    range_header = f"bytes={offset}-{offset + length - 1}"
    for attempt in range(1, MAX_RANGE_ATTEMPTS + 1):
        try:
            return _attempt_fetch_range(session, url, range_header, on_chunk, chunk_size)
        except _RetryableFailure as failure:
            if attempt == MAX_RANGE_ATTEMPTS:
                raise failure.final from failure
            delay = backoff_delay(attempt, failure.response, RANGE_RETRY_BASE_DELAY)
            _warn_retry(retry_label, failure.detail, attempt, delay)
            time.sleep(delay)
