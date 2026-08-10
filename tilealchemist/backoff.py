import random


def _server_requested_delay(response):
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return None
    try:
        return float(retry_after)
    except ValueError:
        return None


def _jittered_backoff(attempt, base_delay):
    # Jittered so many concurrently-running workers throttled in the same
    # burst don't retry in lockstep and immediately reproduce the same burst.
    return base_delay * (2 ** (attempt - 1)) * random.uniform(1.0, 1.5)


def backoff_delay(attempt, response, base_delay):
    return _server_requested_delay(response) or _jittered_backoff(attempt, base_delay)
