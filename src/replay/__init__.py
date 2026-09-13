"""
src/replay

Replay engine package for DATA-SERVICE 2.0.

Provides the ``ReplayEngine`` class and ``ReplaySession`` Pydantic model that
serve historical tick data at a configurable playback speed, emitting the same
response envelope as live data (Requirement 23.3).

The response envelope includes ``dataSourceType: "HISTORICAL"``, ``provenance``,
and ``quality`` fields — identical in shape to live responses so that consumers
cannot distinguish replay from live at the schema level.
"""

from __future__ import annotations

from src.replay.replay_engine import ReplayEngine, ReplaySession, ReplayStatus

__all__ = ["ReplayEngine", "ReplaySession", "ReplayStatus"]
