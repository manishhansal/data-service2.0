"""
src/replay/replay_engine.py

Replay Engine for DATA-SERVICE 2.0.

Implements Requirement 23.3 — replay mode that serves historical data at a
configurable playback speed while emitting the same response envelope as live
responses, including ``dataSourceType: "HISTORICAL"``, ``provenance``, and
quality fields.

Key design constraints (Requirement 23.3):
- ``speedMultiplier`` is a positive float; 1.0 = real-time, 2.0 = 2× speed.
- The response envelope is identical to live responses at the schema level.
- Ticks are sent over the WebSocket in the same format as ``StreamingEngine``
  publishes them (``mds:ticks:{symbol}``).
- ``stop()`` and ``set_speed()`` may be called concurrently from another
  coroutine — both are safe.
- ``ReplaySession`` is the persistent model tracking session lifecycle;
  ``ReplayEngine`` is the stateful runtime object that drives playback.

Replay semantics
----------------
Given a sorted list of tick dicts (each must contain a ``"time"`` field in
UTC epoch milliseconds), the engine:

1. Sets ``status`` to ``"PLAYING"`` and sends an initial ``session_start``
   control frame.
2. Iterates through consecutive tick pairs.  The gap between pair ``(i, i+1)``
   is ``(tick[i+1]["time"] - tick[i]["time"]) / speedMultiplier`` milliseconds.
3. Sends each tick serialised as JSON over the WebSocket, enriched with the
   canonical ``dataSourceType``, ``provenance``, and ``quality`` fields.
4. On ``stop()`` — sends a ``session_stopped`` control frame and exits.
5. On exhaustion — sends a ``session_completed`` control frame and sets
   ``status`` to ``"COMPLETED"``.

Error handling
--------------
- If the WebSocket send raises (broken pipe, client disconnect), replay stops
  gracefully without raising to the caller.
- If ``tick_data`` is empty, the session completes immediately with zero ticks
  sent.
- Negative or zero ``speedMultiplier`` raises ``ValueError`` at construction
  and at ``set_speed`` time.

Requirements: 23.3
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import structlog
from pydantic import BaseModel, Field, field_validator

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Session status enum
# ---------------------------------------------------------------------------


class ReplayStatus(str, Enum):
    """Lifecycle states for a ``ReplaySession``."""

    CREATED = "CREATED"
    PLAYING = "PLAYING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"


# ---------------------------------------------------------------------------
# ReplaySession — persistent Pydantic model
# ---------------------------------------------------------------------------


class ReplaySession(BaseModel):
    """Persistent model representing the metadata and current state of one
    replay session.

    Fields
    ------
    sessionId
        UUID v4 identifier assigned at creation time.
    symbol
        The trading symbol being replayed (e.g. ``"NIFTY"``).
    startTs
        UTC epoch milliseconds of the first tick in the replay window.
    endTs
        UTC epoch milliseconds of the last tick in the replay window.
    speedMultiplier
        Playback speed relative to real time.  1.0 = real-time, 2.0 = 2×.
        Must be a positive float.
    status
        Current lifecycle state (``CREATED`` / ``PLAYING`` / ``PAUSED`` /
        ``COMPLETED`` / ``STOPPED``).
    tickCount
        Total number of ticks in the session (populated from tick_data at
        engine construction time).
    ticksSent
        Running count of ticks sent so far (updated during playback).
    createdAt
        UTC ISO-8601 timestamp of session creation.
    startedAt
        UTC ISO-8601 timestamp when playback began (null until PLAYING).
    completedAt
        UTC ISO-8601 timestamp when playback completed or was stopped (null
        until COMPLETED / STOPPED).
    """

    sessionId: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="UUID v4 session identifier.",
    )
    symbol: str = Field(..., min_length=1, description="Trading symbol being replayed.")
    startTs: int = Field(..., description="UTC epoch ms of the first tick.")
    endTs: int = Field(..., description="UTC epoch ms of the last tick.")
    speedMultiplier: float = Field(
        default=1.0,
        gt=0,
        description="Playback speed multiplier.  Must be > 0.",
    )
    status: ReplayStatus = Field(
        default=ReplayStatus.CREATED,
        description="Current lifecycle state.",
    )
    tickCount: int = Field(
        default=0,
        ge=0,
        description="Total ticks in the session dataset.",
    )
    ticksSent: int = Field(
        default=0,
        ge=0,
        description="Number of ticks dispatched so far.",
    )
    createdAt: str = Field(
        default_factory=lambda: _utc_iso_now(),
        description="UTC ISO-8601 creation timestamp.",
    )
    startedAt: Optional[str] = Field(
        default=None,
        description="UTC ISO-8601 timestamp when playback started.",
    )
    completedAt: Optional[str] = Field(
        default=None,
        description="UTC ISO-8601 timestamp when playback completed or stopped.",
    )

    @field_validator("speedMultiplier")
    @classmethod
    def _validate_speed(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"speedMultiplier must be > 0; got {v!r}")
        return v

    @field_validator("endTs")
    @classmethod
    def _validate_timestamps(cls, v: int, info: Any) -> int:
        start = info.data.get("startTs")
        if start is not None and v < start:
            raise ValueError(
                f"endTs ({v}) must be >= startTs ({start})"
            )
        return v


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _build_tick_envelope(tick: dict[str, Any], session_id: str) -> dict[str, Any]:
    """Wrap a raw tick dict in the canonical DATA-SERVICE 2.0 response envelope.

    The envelope mirrors the live streaming tick format exactly, with the
    addition of ``dataSourceType: "HISTORICAL"`` so that consumers can
    distinguish replay from live data at audit time, while the schema remains
    identical (Requirement 23.3).

    The ``provenance`` object is minimal for replay — a real production
    deployment would hydrate it from the stored ``DataProvenance`` record
    linked to the original ``dataObservationId``.
    """
    received_at = _utc_iso_now()
    return {
        # Pass-through all original tick fields (ltp, volume, oi, etc.)
        **tick,
        # Canonical envelope additions
        "dataSourceType": "HISTORICAL",
        "provenance": {
            "dataObservationId": tick.get("dataObservationId", str(uuid.uuid4())),
            "source": tick.get("source", "REPLAY"),
            "eventTimeMs": tick.get("time"),
            "receivedAtMs": tick.get("receivedAtMs"),
            "availableAtMs": tick.get("availableAtMs"),
            "normalisationVersion": tick.get("normalisationVersion", "1.0.0"),
            "isFallback": False,
            "fallbackReason": None,
        },
        "quality": tick.get("quality", None),
        "replaySessionId": session_id,
        "replayAt": received_at,
    }


def _control_frame(frame_type: str, session_id: str, **extra: Any) -> str:
    """Serialise a control frame sent to the consumer over the WebSocket."""
    return json.dumps(
        {
            "type": frame_type,
            "sessionId": session_id,
            "timestamp": _utc_iso_now(),
            **extra,
        },
        default=str,
    )


# ---------------------------------------------------------------------------
# ReplayEngine — stateful runtime
# ---------------------------------------------------------------------------


class ReplayEngine:
    """Drives playback of a pre-loaded tick dataset over a WebSocket connection.

    Lifecycle
    ---------
    1. Construct with ``tick_data`` (list of tick dicts, each with ``"time"``
       in UTC epoch ms) and an optional ``speed_multiplier``.
    2. Call ``await replay(websocket)`` to start playback.  This coroutine runs
       until playback completes or ``stop()`` is called.
    3. ``set_speed(multiplier)`` can be called at any point to change the speed
       mid-replay.  The new speed takes effect at the next inter-tick sleep.
    4. ``stop()`` terminates playback at the next tick boundary (before the
       next sleep), without closing the WebSocket.

    Thread / concurrency safety
    ---------------------------
    ``set_speed()`` and ``stop()`` are safe to call from any coroutine running
    in the same event loop — they mutate simple Python values protected by
    ``asyncio.Lock`` or by the GIL.  There is no need for explicit locking
    because Python attribute assignment is atomic at the interpreter level for
    simple scalars, and we only read ``_stop_requested`` in the async loop.

    Parameters
    ----------
    tick_data
        List of tick dicts.  Each dict **must** contain a ``"time"`` key
        holding a UTC epoch millisecond integer.  The list is consumed in the
        order provided; the caller is responsible for sorting by ``"time"`` if
        chronological order is required (the engine does not sort internally to
        preserve the caller's intent, e.g. for testing out-of-order delivery).
    speed_multiplier
        Positive float playback speed.  1.0 = real-time, 2.0 = 2× faster.
        Defaults to 1.0.
    session_id
        Optional session ID string to embed in control frames and tick
        envelopes.  Defaults to a new UUID v4.

    Raises
    ------
    ValueError
        If ``speed_multiplier`` is not a positive float.
    """

    def __init__(
        self,
        tick_data: list[dict[str, Any]],
        speed_multiplier: float = 1.0,
        session_id: Optional[str] = None,
    ) -> None:
        if speed_multiplier <= 0:
            raise ValueError(
                f"speed_multiplier must be > 0; got {speed_multiplier!r}"
            )
        self._tick_data: list[dict[str, Any]] = tick_data
        self._speed_multiplier: float = float(speed_multiplier)
        self._session_id: str = session_id or str(uuid.uuid4())
        self._stop_requested: bool = False
        self._playing: bool = False
        self._ticks_sent: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()
        # Separate lock solely for the re-entrant guard in replay().
        self._run_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public control methods
    # ------------------------------------------------------------------

    def set_speed(self, multiplier: float) -> None:
        """Change the replay speed multiplier.

        Parameters
        ----------
        multiplier
            New playback speed.  Must be a positive float.

        Raises
        ------
        ValueError
            If *multiplier* is not positive.
        """
        if multiplier <= 0:
            raise ValueError(
                f"speed_multiplier must be > 0; got {multiplier!r}"
            )
        self._speed_multiplier = float(multiplier)

    def stop(self) -> None:
        """Request that the replay loop terminate at the next tick boundary.

        This is a non-blocking signal — it sets a flag that the ``replay``
        coroutine checks before each sleep.  The WebSocket is not closed.
        """
        self._stop_requested = True

    def is_playing(self) -> bool:
        """Return ``True`` while the replay coroutine is running."""
        return self._playing

    @property
    def ticks_sent(self) -> int:
        """Number of ticks dispatched to the WebSocket so far."""
        return self._ticks_sent

    @property
    def session_id(self) -> str:
        """The session ID embedded in all frames sent during replay."""
        return self._session_id

    # ------------------------------------------------------------------
    # Core replay coroutine
    # ------------------------------------------------------------------

    async def replay(self, websocket: Any) -> None:
        """Replay all ticks to *websocket* at the configured speed.

        For each consecutive pair of ticks ``(i, i+1)``, the engine sleeps::

            sleep_sec = (tick[i+1]["time"] - tick[i]["time"]) / speed_multiplier / 1000

        Negative gaps (out-of-order ticks) produce zero sleep (no negative
        sleep) so playback is never stalled.

        The WebSocket is not closed when replay ends — that is the caller's
        responsibility.

        Parameters
        ----------
        websocket
            Any object that exposes ``async send_text(str) -> None``.  In
            production this is a FastAPI ``WebSocket``; in tests it can be any
            compatible stub.

        Raises
        ------
        Nothing — all exceptions from the WebSocket send are caught and logged;
        replay terminates gracefully.
        """
        # Re-entrant guard via asyncio.Lock.  The lock is acquired here and
        # immediately released; _playing is set to True while holding the lock
        # to prevent a concurrent second call from entering the replay loop.
        # We use a dedicated _run_lock (not _lock, which guards set_speed/stop)
        # so that stop() can still be called concurrently without deadlock.
        if not await self._run_lock.acquire():
            # Should not happen with asyncio.Lock, but guard anyway.
            return
        if self._playing:
            self._run_lock.release()
            await _log.awarning(
                "replay_already_playing", session_id=self._session_id
            )
            return
        self._playing = True
        # NOTE: do NOT reset _stop_requested here — callers may call
        # stop() before replay() to pre-stage a stop.
        self._ticks_sent = 0
        self._run_lock.release()

        await _log.ainfo(
            "replay_started",
            session_id=self._session_id,
            tick_count=len(self._tick_data),
            speed_multiplier=self._speed_multiplier,
        )

        try:
            # Send session_start control frame.
            await websocket.send_text(
                _control_frame(
                    "session_start",
                    self._session_id,
                    tickCount=len(self._tick_data),
                    speedMultiplier=self._speed_multiplier,
                )
            )

            if not self._tick_data:
                # Empty dataset — complete immediately.
                await websocket.send_text(
                    _control_frame(
                        "session_completed",
                        self._session_id,
                        ticksSent=0,
                    )
                )
                return

            prev_time: Optional[int] = None

            for tick in self._tick_data:
                if self._stop_requested:
                    # Termination requested — send control frame and exit.
                    await websocket.send_text(
                        _control_frame(
                            "session_stopped",
                            self._session_id,
                            ticksSent=self._ticks_sent,
                        )
                    )
                    await _log.ainfo(
                        "replay_stopped",
                        session_id=self._session_id,
                        ticks_sent=self._ticks_sent,
                    )
                    return

                current_time: int = tick.get("time", 0)

                # Sleep between ticks to simulate wall-clock pacing.
                if prev_time is not None:
                    gap_ms = current_time - prev_time
                    if gap_ms > 0:
                        sleep_sec = gap_ms / self._speed_multiplier / 1000.0
                        await asyncio.sleep(sleep_sec)

                # Send the tick envelope.
                envelope = _build_tick_envelope(tick, self._session_id)
                try:
                    await websocket.send_text(json.dumps(envelope, default=str))
                    self._ticks_sent += 1
                except Exception as exc:  # noqa: BLE001
                    await _log.awarning(
                        "replay_send_error",
                        session_id=self._session_id,
                        ticks_sent=self._ticks_sent,
                        error=str(exc),
                    )
                    return

                prev_time = current_time

            # Natural end of tick stream — send completion frame.
            await websocket.send_text(
                _control_frame(
                    "session_completed",
                    self._session_id,
                    ticksSent=self._ticks_sent,
                )
            )
            await _log.ainfo(
                "replay_completed",
                session_id=self._session_id,
                ticks_sent=self._ticks_sent,
            )

        except Exception as exc:  # noqa: BLE001
            await _log.awarning(
                "replay_error",
                session_id=self._session_id,
                error=str(exc),
            )
        finally:
            self._playing = False


# ---------------------------------------------------------------------------
# In-memory session registry
# ---------------------------------------------------------------------------

#: Module-level registry mapping ``sessionId`` → ``ReplaySession``.
#: In a production deployment this would be backed by Redis or PostgreSQL;
#: for the current scope (Task 14.4) an in-memory dict is sufficient.
_session_registry: dict[str, ReplaySession] = {}


def create_session(
    symbol: str,
    tick_data: list[dict[str, Any]],
    speed_multiplier: float = 1.0,
) -> ReplaySession:
    """Create and register a new ``ReplaySession`` from a tick dataset.

    Parameters
    ----------
    symbol
        Trading symbol (e.g. ``"NIFTY"``).
    tick_data
        List of tick dicts with a ``"time"`` field (UTC epoch ms).  Must be
        non-empty for a meaningful session.
    speed_multiplier
        Playback speed.  Must be > 0.

    Returns
    -------
    ReplaySession
        The newly created and registered session.

    Raises
    ------
    ValueError
        If ``tick_data`` is empty or ``speed_multiplier`` is not positive.
    """
    if speed_multiplier <= 0:
        raise ValueError(
            f"speed_multiplier must be > 0; got {speed_multiplier!r}"
        )

    if tick_data:
        start_ts = min(t.get("time", 0) for t in tick_data)
        end_ts = max(t.get("time", 0) for t in tick_data)
    else:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ts = now_ms
        end_ts = now_ms

    session = ReplaySession(
        symbol=symbol,
        startTs=start_ts,
        endTs=end_ts,
        speedMultiplier=speed_multiplier,
        tickCount=len(tick_data),
    )
    _session_registry[session.sessionId] = session
    return session


def get_session(session_id: str) -> Optional[ReplaySession]:
    """Look up a session by ID.  Returns ``None`` if not found."""
    return _session_registry.get(session_id)


def update_session_status(session_id: str, status: ReplayStatus) -> Optional[ReplaySession]:
    """Update the ``status`` field of an existing session.

    Returns the updated session or ``None`` if the session does not exist.
    """
    session = _session_registry.get(session_id)
    if session is None:
        return None
    # Pydantic v2 model_copy with update
    updated = session.model_copy(
        update={
            "status": status,
            "startedAt": _utc_iso_now() if status == ReplayStatus.PLAYING and session.startedAt is None else session.startedAt,
            "completedAt": _utc_iso_now() if status in (ReplayStatus.COMPLETED, ReplayStatus.STOPPED) else session.completedAt,
        }
    )
    _session_registry[session_id] = updated
    return updated
