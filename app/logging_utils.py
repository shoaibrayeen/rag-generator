"""Step logging shared by the pipeline, extractors, retrieval and API.

Every log line carries the step name and, where known, the job/document ids, so a single
`grep doc=<id>` shows the full story of one file. `timed()` logs start, end and elapsed ms.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from app.config import settings


def configure_logging() -> None:
    logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def ctx(**ids) -> str:
    """Format ids as `job=… doc=… file=…` for log lines."""
    return " ".join(f"{k}={v}" for k, v in ids.items() if v not in (None, ""))


@contextmanager
def timed(log: logging.Logger, step: str, **ids):
    """Log `step start`, then `step done in N ms` (or `step FAILED`) around a block."""
    tag = ctx(**ids)
    log.info("[%s] start %s", step, tag)
    t0 = time.perf_counter()
    try:
        yield
    except Exception as exc:
        log.warning("[%s] FAILED after %d ms %s: %s: %s", step, (time.perf_counter() - t0) * 1000, tag,
                    type(exc).__name__, exc)
        raise
    log.info("[%s] done in %d ms %s", step, (time.perf_counter() - t0) * 1000, tag)
