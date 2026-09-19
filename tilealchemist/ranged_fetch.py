"""HTTP Range fetching against the source PMTiles archive."""
import sys
import time

import requests

from tilealchemist.backoff import backoff_delay
from tilealchemist.throttle import UpdateLineThrottle

# Covers the transient CDN failures of a cold-cache stampede; see docs/ARCHITECTURE.md.
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

    def __init__(self, total_bytes, interval, label):
        self.total_bytes = total_bytes
        self.label = label
        self.throttle = UpdateLineThrottle(interval, fire_immediately=True)

    def update(self, downloaded):
        """`downloaded` is the attempt's running total, so a retry rewinds rather than doubles."""
        if not self.throttle.due():
            return
        percent = (100 * downloaded / self.total_bytes) if self.total_bytes else 100.0
        print(f"update: downloading {self.label}: "
              f"{downloaded}/{self.total_bytes} bytes ({percent:.1f}%)",
              file=sys.stderr)


class _RetryableFailure(Exception):

    def __init__(self, detail, response, final):
        super().__init__(detail)
        self.detail = detail
        self.response = response
        self.final = final


def _attempt_fetch_range(session, url, range_header, on_chunk, chunk_size):
    # Leaving the `with` on a raise returns the connection before the caller's backoff sleeps.
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
            # No response left to read a Retry-After from, so the backoff runs on jitter alone.
            raise _RetryableFailure(
                f"connection dropped after {downloaded} bytes "
                f"({error.__class__.__name__})", None, error) from error


def _warn_retry(retry_label, detail, attempt, delay):
    print(f"::warning title={retry_label} retry::{detail} "
          f"(attempt {attempt}/{MAX_RANGE_ATTEMPTS}), retrying in {delay:.0f}s",
          file=sys.stderr)


def fetch_range(session, url, offset, length, retry_label,
                on_chunk=None, chunk_size=1024 * 1024):
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
