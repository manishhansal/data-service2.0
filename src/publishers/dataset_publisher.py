"""
src/publishers/dataset_publisher.py

DatasetPublisher — high-level wrapper around StreamingEngine that adds
retry logic (3 retries with exponential backoff) for dataset-ready event
publishing (Task 8.7 / Requirement 15.3).

Usage
-----
::

    publisher = DatasetPublisher(engine=streaming_engine)
    msg_id = await publisher.publish(
        market="NSE",
        symbol="RELIANCE",
        interval="1d",
        date="2024-01-15",
        record_count=375,
    )

The ``publish`` method tries up to 3 times (initial attempt + 2 retries)
with exponential back-off (1 s, 2 s between retries).  If all attempts
fail it logs a structured error and re-raises the last exception so callers
can decide whether to surface the error or degrade gracefully.

Requirements: 15.3 (dataset-ready event), 8.7
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import structlog

from src.engines.streaming_engine import StreamingEngine

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

__all__ = ["DatasetPublisher"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_RETRIES: int = 3          # total attempts (1 initial + 2 retries)
_BACKOFF_BASE_SECS: float = 1.0  # wait 1s before retry-1, 2s before retry-2


class DatasetPublisher:
    """Wraps :class:`~src.engines.streaming_engine.StreamingEngine` with
    retry logic for publishing dataset-ready events.

    Parameters
    ----------
    engine:
        An initialised :class:`~src.engines.streaming_engine.StreamingEngine`
        instance.  May be ``None`` during testing when the caller intends to
        mock or replace the engine after construction.
    max_retries:
        Total number of publish attempts (1 initial + *n-1* retries).
        Minimum 1, maximum 10.  Defaults to 3.
    backoff_base_secs:
        Base back-off duration in seconds.  Delay before retry *i* is
        ``backoff_base_secs * 2 ** (i - 1)``.  Defaults to 1.0 s.
    """

    def __init__(
        self,
        engine: Optional[StreamingEngine] = None,
        *,
        max_retries: int = _MAX_RETRIES,
        backoff_base_secs: float = _BACKOFF_BASE_SECS,
    ) -> None:
        if not 1 <= max_retries <= 10:
            raise ValueError(f"max_retries must be between 1 and 10; got {max_retries}")
        if backoff_base_secs <= 0:
            raise ValueError(f"backoff_base_secs must be positive; got {backoff_base_secs}")

        self._engine: Optional[StreamingEngine] = engine
        self._max_retries: int = max_retries
        self._backoff_base_secs: float = backoff_base_secs

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def publish(
        self,
        *,
        market: str,
        symbol: str,
        interval: str,
        date: str,
        record_count: int,
    ) -> str:
        """Publish a dataset-ready event with retry and exponential back-off.

        Parameters
        ----------
        market:
            Market identifier, e.g. ``"NSE"``, ``"NFO"``, ``"CRYPTO"``.
        symbol:
            Trading symbol, e.g. ``"RELIANCE"``, ``"NIFTY"``, ``"BTC"``.
        interval:
            Canonical interval string, e.g. ``"1m"``, ``"5m"``, ``"1d"``.
        date:
            Session / reference date as ``"YYYY-MM-DD"``.
        record_count:
            Number of records in the completed dataset.

        Returns
        -------
        str
            Stream entry ID returned by Redis on success.

        Raises
        ------
        RuntimeError
            When :attr:`engine` is ``None``.
        Exception
            Re-raises the last exception if all retry attempts fail.
        """
        if self._engine is None:
            raise RuntimeError(
                "DatasetPublisher has no StreamingEngine configured. "
                "Assign one via DatasetPublisher(engine=...) before calling publish()."
            )

        last_exc: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            try:
                msg_id = await self._engine.publish_dataset_ready_simple(
                    market=market,
                    symbol=symbol,
                    interval=interval,
                    date=date,
                    record_count=record_count,
                )

                if attempt > 1:
                    await logger.ainfo(
                        "dataset_ready_publish_succeeded_after_retry",
                        market=market,
                        symbol=symbol,
                        interval=interval,
                        date=date,
                        record_count=record_count,
                        attempt=attempt,
                        msg_id=msg_id,
                    )
                else:
                    await logger.adebug(
                        "dataset_ready_published",
                        market=market,
                        symbol=symbol,
                        interval=interval,
                        date=date,
                        record_count=record_count,
                        msg_id=msg_id,
                    )

                return msg_id

            except Exception as exc:  # noqa: BLE001
                last_exc = exc

                if attempt < self._max_retries:
                    delay = self._backoff_base_secs * (2 ** (attempt - 1))
                    await logger.awarning(
                        "dataset_ready_publish_failed_retrying",
                        market=market,
                        symbol=symbol,
                        interval=interval,
                        date=date,
                        record_count=record_count,
                        attempt=attempt,
                        max_retries=self._max_retries,
                        retry_delay_secs=delay,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                else:
                    await logger.aerror(
                        "dataset_ready_publish_exhausted_retries",
                        market=market,
                        symbol=symbol,
                        interval=interval,
                        date=date,
                        record_count=record_count,
                        attempt=attempt,
                        max_retries=self._max_retries,
                        error=str(exc),
                    )

        # All attempts failed — re-raise so callers can handle gracefully.
        assert last_exc is not None  # guaranteed: loop ran at least once
        raise last_exc

    # ------------------------------------------------------------------
    # Convenience: publish from a raw event dict
    # ------------------------------------------------------------------

    async def publish_event(self, event_data: dict[str, Any]) -> str:
        """Publish a dataset-ready event from a pre-built dict with retry.

        The dict must contain keys: ``market``, ``symbol``, ``interval``,
        ``date``, ``record_count``.

        Parameters
        ----------
        event_data:
            Mapping with the required keys.  Extra keys are passed through.

        Returns
        -------
        str
            Stream entry ID on success.

        Raises
        ------
        KeyError
            When any required key is absent from *event_data*.
        RuntimeError
            When :attr:`engine` is ``None``.
        """
        return await self.publish(
            market=str(event_data["market"]),
            symbol=str(event_data["symbol"]),
            interval=str(event_data["interval"]),
            date=str(event_data["date"]),
            record_count=int(event_data["record_count"]),
        )
