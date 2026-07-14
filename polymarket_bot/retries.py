"""Shared retry helpers with exponential backoff."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

import config

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetriableError(Exception):
    """Raised when an operation should be retried by the caller/wrapper."""


def with_retry(
    fn: Callable[[], T],
    *,
    op_name: str,
    attempts: int | None = None,
    base_seconds: float | None = None,
    max_seconds: float | None = None,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """
    Run ``fn`` with exponential backoff + jitter.

    Permanent failures should raise exceptions outside ``retry_on`` (or be
    filtered by the caller). Transient API/network errors are retried.
    """
    attempts = attempts if attempts is not None else config.API_RETRY_ATTEMPTS
    base = base_seconds if base_seconds is not None else config.API_RETRY_BASE_SECONDS
    max_s = max_seconds if max_seconds is not None else config.API_RETRY_MAX_SECONDS

    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:  # noqa: PERF203 — clarity over micro-opt
            last_exc = exc
            if attempt >= attempts:
                break
            delay = min(max_s, base * (2 ** (attempt - 1)))
            delay *= 0.5 + random.random()  # full-jitter-ish
            logger.warning(
                "%s failed (attempt %s/%s): %s; retrying in %.2fs",
                op_name,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc
