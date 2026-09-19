"""Retry delay for a throttled or failed ranged request."""
import random


def _server_requested_delay(response):
    if response is None:
        return None
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return None
    try:
        return float(retry_after)
    except ValueError:
        return None


def _jittered_backoff(attempt, base_delay):
    # Jitter breaks the lockstep that would reproduce the throttling burst.
    return base_delay * (2 ** (attempt - 1)) * random.uniform(1.0, 1.5)


def backoff_delay(attempt, response, base_delay):
    return _server_requested_delay(response) or _jittered_backoff(attempt, base_delay)
