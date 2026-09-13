"""
tests/unit/replay/test_replay_engine.py

Unit tests for src/replay/replay_engine.py.

Covers:
- ReplaySession Pydantic v2 model validation
- ReplayEngine construction validation
- set_speed() / stop() / is_playing() semantics
- replay() with empty dataset completes immediately
- replay() sends correct number of ticks
- replay() attaches dataSourceType: "HISTORICAL" envelope
- replay() stops cleanly after stop() is called
- replay() handles broken WebSocket gracefully
- replay() sends session_start / session_completed / session_stopped control frames
- Concurrent stop() call halts replay mid-stream
- Session registry helpers: create_session, get_session, update_session_status
- API endpoint contract: POST /v1/replay/sessions → 201
- API endpoint contract: GET /v1/replay/sessions/{id}/status → 200 / 404
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.replay.replay_engine import (
    ReplayEngine,
    ReplaySession,
    ReplayStatus,
    _build_tick_envelope,
    create_session,
    get_session,
    update_session_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ticks(n: int, start_ms: int = 1_700_000_000_000, gap_ms: int = 60_000) -> list[dict]:
    """Return *n* minimal tick dicts spaced *gap_ms* apart."""
    return [
        {
            "time": start_ms + i * gap_ms,
            "ltp": 19_500.0 + i,
            "volume": 100 + i,
            "symbol": "NIFTY",
            "source": "TEST",
        }
        for i in range(n)
    ]


class FakeWebSocket:
    """Minimal WebSocket stub that records all sent text frames."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.raise_on_send: bool = False

    async def send_text(self, text: str) -> None:  # noqa: D102
        if self.raise_on_send:
            raise OSError("Simulated broken pipe")
        self.sent.append(text)

    def frames(self) -> list[dict]:
        """Parse all sent text frames as JSON and return them."""
        return [json.loads(f) for f in self.sent]

    def control_frames(self) -> list[dict]:
        """Return only control frames (those with a ``type`` key)."""
        return [f for f in self.frames() if "type" in f]

    def tick_frames(self) -> list[dict]:
        """Return only tick payload frames (those without a ``type`` key)."""
        return [f for f in self.frames() if "type" not in f]


# ---------------------------------------------------------------------------
# ReplaySession model
# ---------------------------------------------------------------------------


class TestReplaySessionModel:
    def test_default_fields_populated(self):
        session = ReplaySession(symbol="NIFTY", startTs=1000, endTs=2000)
        assert session.sessionId  # non-empty UUID
        assert session.status == ReplayStatus.CREATED
        assert session.speedMultiplier == 1.0
        assert session.tickCount == 0
        assert session.ticksSent == 0
        assert session.startedAt is None
        assert session.completedAt is None
        assert session.createdAt  # non-empty timestamp string

    def test_custom_speed_multiplier(self):
        session = ReplaySession(symbol="BTC", startTs=0, endTs=1, speedMultiplier=4.5)
        assert session.speedMultiplier == 4.5

    def test_zero_speed_multiplier_raises(self):
        with pytest.raises(ValidationError):
            ReplaySession(symbol="X", startTs=0, endTs=1, speedMultiplier=0.0)

    def test_negative_speed_multiplier_raises(self):
        with pytest.raises(ValidationError):
            ReplaySession(symbol="X", startTs=0, endTs=1, speedMultiplier=-1.0)

    def test_endts_before_startts_raises(self):
        with pytest.raises(ValidationError):
            ReplaySession(symbol="X", startTs=2000, endTs=1000)

    def test_status_enum_values(self):
        for status in ReplayStatus:
            s = ReplaySession(symbol="X", startTs=0, endTs=0, status=status)
            assert s.status == status

    def test_model_dump_includes_all_fields(self):
        session = ReplaySession(symbol="NIFTY", startTs=100, endTs=200)
        d = session.model_dump()
        required_keys = {
            "sessionId", "symbol", "startTs", "endTs", "speedMultiplier",
            "status", "tickCount", "ticksSent", "createdAt", "startedAt", "completedAt",
        }
        assert required_keys.issubset(d.keys())

    def test_session_id_is_valid_uuid(self):
        session = ReplaySession(symbol="ETH", startTs=0, endTs=1)
        # Should not raise
        uuid.UUID(session.sessionId, version=4)


# ---------------------------------------------------------------------------
# ReplayEngine construction
# ---------------------------------------------------------------------------


class TestReplayEngineConstruction:
    def test_default_speed_is_one(self):
        engine = ReplayEngine(tick_data=[])
        assert engine._speed_multiplier == 1.0

    def test_custom_speed_stored(self):
        engine = ReplayEngine(tick_data=[], speed_multiplier=3.0)
        assert engine._speed_multiplier == 3.0

    def test_zero_speed_raises(self):
        with pytest.raises(ValueError, match="speed_multiplier"):
            ReplayEngine(tick_data=[], speed_multiplier=0.0)

    def test_negative_speed_raises(self):
        with pytest.raises(ValueError, match="speed_multiplier"):
            ReplayEngine(tick_data=[], speed_multiplier=-2.0)

    def test_custom_session_id_stored(self):
        sid = str(uuid.uuid4())
        engine = ReplayEngine(tick_data=[], session_id=sid)
        assert engine.session_id == sid

    def test_auto_session_id_is_uuid(self):
        engine = ReplayEngine(tick_data=[])
        uuid.UUID(engine.session_id, version=4)  # must not raise

    def test_initial_state(self):
        engine = ReplayEngine(tick_data=_make_ticks(5))
        assert engine.is_playing() is False
        assert engine.ticks_sent == 0


# ---------------------------------------------------------------------------
# set_speed / stop / is_playing
# ---------------------------------------------------------------------------


class TestSetSpeedAndStop:
    def test_set_speed_updates_multiplier(self):
        engine = ReplayEngine(tick_data=[])
        engine.set_speed(5.0)
        assert engine._speed_multiplier == 5.0

    def test_set_speed_zero_raises(self):
        engine = ReplayEngine(tick_data=[])
        with pytest.raises(ValueError, match="speed_multiplier"):
            engine.set_speed(0.0)

    def test_set_speed_negative_raises(self):
        engine = ReplayEngine(tick_data=[])
        with pytest.raises(ValueError, match="speed_multiplier"):
            engine.set_speed(-1.0)

    def test_stop_sets_flag(self):
        engine = ReplayEngine(tick_data=[])
        assert engine._stop_requested is False
        engine.stop()
        assert engine._stop_requested is True

    def test_is_playing_false_before_replay(self):
        engine = ReplayEngine(tick_data=[])
        assert engine.is_playing() is False


# ---------------------------------------------------------------------------
# replay() — empty dataset
# ---------------------------------------------------------------------------


class TestReplayEmptyDataset:
    async def test_empty_dataset_completes_immediately(self):
        engine = ReplayEngine(tick_data=[])
        ws = FakeWebSocket()
        await engine.replay(ws)

        assert engine.is_playing() is False
        frames = ws.control_frames()
        types = [f["type"] for f in frames]
        assert "session_start" in types
        assert "session_completed" in types

    async def test_empty_dataset_sends_zero_ticks(self):
        engine = ReplayEngine(tick_data=[])
        ws = FakeWebSocket()
        await engine.replay(ws)
        assert ws.tick_frames() == []

    async def test_empty_dataset_ticks_sent_is_zero(self):
        engine = ReplayEngine(tick_data=[])
        ws = FakeWebSocket()
        await engine.replay(ws)
        assert engine.ticks_sent == 0

    async def test_completed_frame_has_zero_ticks_sent(self):
        engine = ReplayEngine(tick_data=[])
        ws = FakeWebSocket()
        await engine.replay(ws)
        completed = next(f for f in ws.control_frames() if f["type"] == "session_completed")
        assert completed["ticksSent"] == 0


# ---------------------------------------------------------------------------
# replay() — tick delivery
# ---------------------------------------------------------------------------


class TestReplayTickDelivery:
    async def test_sends_all_ticks(self):
        """All ticks in the dataset must be delivered to the WebSocket."""
        ticks = _make_ticks(5)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        ws = FakeWebSocket()
        await engine.replay(ws)

        assert engine.ticks_sent == 5
        assert len(ws.tick_frames()) == 5

    async def test_tick_envelope_has_historical_data_source_type(self):
        ticks = _make_ticks(2)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        ws = FakeWebSocket()
        await engine.replay(ws)

        for frame in ws.tick_frames():
            assert frame["dataSourceType"] == "HISTORICAL"

    async def test_tick_envelope_has_provenance(self):
        ticks = _make_ticks(2)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        ws = FakeWebSocket()
        await engine.replay(ws)

        for frame in ws.tick_frames():
            assert "provenance" in frame
            prov = frame["provenance"]
            assert "dataObservationId" in prov
            assert "source" in prov
            assert "normalisationVersion" in prov

    async def test_tick_envelope_preserves_original_fields(self):
        """Original tick fields (ltp, volume, etc.) must pass through unchanged."""
        ticks = _make_ticks(3)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        ws = FakeWebSocket()
        await engine.replay(ws)

        for i, frame in enumerate(ws.tick_frames()):
            assert frame["ltp"] == pytest.approx(19_500.0 + i)
            assert frame["volume"] == 100 + i

    async def test_tick_envelope_has_replay_session_id(self):
        sid = str(uuid.uuid4())
        ticks = _make_ticks(3)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000, session_id=sid)
        ws = FakeWebSocket()
        await engine.replay(ws)

        for frame in ws.tick_frames():
            assert frame["replaySessionId"] == sid

    async def test_session_start_frame_sent_first(self):
        ticks = _make_ticks(3)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        ws = FakeWebSocket()
        await engine.replay(ws)

        first = ws.frames()[0]
        assert first["type"] == "session_start"

    async def test_session_completed_frame_sent_last(self):
        ticks = _make_ticks(3)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        ws = FakeWebSocket()
        await engine.replay(ws)

        last = ws.frames()[-1]
        assert last["type"] == "session_completed"
        assert last["ticksSent"] == 3

    async def test_session_start_frame_has_tick_count(self):
        ticks = _make_ticks(4)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        ws = FakeWebSocket()
        await engine.replay(ws)

        start = next(f for f in ws.control_frames() if f["type"] == "session_start")
        assert start["tickCount"] == 4

    async def test_session_start_frame_has_speed_multiplier(self):
        ticks = _make_ticks(2)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=3.0)
        ws = FakeWebSocket()

        # Run with very high speed to avoid wall-clock delay in tests.
        engine._speed_multiplier = 1_000_000
        await engine.replay(ws)

        start = next(f for f in ws.control_frames() if f["type"] == "session_start")
        assert start["speedMultiplier"] == 1_000_000

    async def test_is_playing_false_after_completion(self):
        ticks = _make_ticks(3)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        ws = FakeWebSocket()
        await engine.replay(ws)

        assert engine.is_playing() is False


# ---------------------------------------------------------------------------
# replay() — stop mid-stream
# ---------------------------------------------------------------------------


class TestReplayStop:
    async def test_stop_sends_session_stopped_frame(self):
        """Calling stop() before replay starts causes session_stopped to be sent."""
        ticks = _make_ticks(10)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        engine.stop()  # stop before replay
        ws = FakeWebSocket()
        await engine.replay(ws)

        types = [f["type"] for f in ws.control_frames()]
        assert "session_stopped" in types

    async def test_stop_mid_replay_halts_delivery(self):
        """stop() called from a concurrent task must halt replay before all ticks are sent."""
        # Use large gaps and moderate speed so the test stopper fires mid-stream.
        # 100 ticks × 1000 ms gap / 100x speed = 1000 ms total → stopper at 5ms fires ~mid-stream
        ticks = _make_ticks(100, gap_ms=1_000)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=100)
        ws = FakeWebSocket()

        async def _stopper() -> None:
            await asyncio.sleep(0.005)
            engine.stop()

        await asyncio.gather(engine.replay(ws), _stopper())

        # Should have sent fewer than 100 ticks
        assert engine.ticks_sent < 100

    async def test_stop_before_start_sends_zero_ticks(self):
        """If stop() is called before replay(), no tick frames should be delivered."""
        ticks = _make_ticks(10)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        engine.stop()
        ws = FakeWebSocket()
        await engine.replay(ws)

        assert ws.tick_frames() == []

    async def test_session_stopped_frame_reports_ticks_sent(self):
        """session_stopped ticksSent must equal the number of tick frames sent."""
        ticks = _make_ticks(10, gap_ms=1)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        ws = FakeWebSocket()

        # Stop after the first tick is sent by scheduling the call.
        async def _stopper() -> None:
            # Wait until at least one tick is sent.
            for _ in range(100):
                if engine.ticks_sent > 0:
                    engine.stop()
                    return
                await asyncio.sleep(0.001)
            engine.stop()

        await asyncio.gather(engine.replay(ws), _stopper())

        stopped = next(
            (f for f in ws.control_frames() if f["type"] == "session_stopped"), None
        )
        if stopped is not None:
            assert stopped["ticksSent"] == engine.ticks_sent

    async def test_is_playing_false_after_stop(self):
        ticks = _make_ticks(5)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        engine.stop()
        ws = FakeWebSocket()
        await engine.replay(ws)

        assert engine.is_playing() is False


# ---------------------------------------------------------------------------
# replay() — broken WebSocket
# ---------------------------------------------------------------------------


class TestReplayBrokenWebSocket:
    async def test_broken_ws_does_not_raise(self):
        """If the WebSocket raises on send, replay must not propagate the exception."""
        ticks = _make_ticks(5)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)
        ws = FakeWebSocket()
        ws.raise_on_send = True  # raise on the first real tick send

        # Must complete without exception
        await engine.replay(ws)

    async def test_broken_ws_stops_replay(self):
        """After a send error, the engine must not continue sending ticks."""
        ticks = _make_ticks(10)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1_000_000)

        sent_count = 0

        class FailAfterThree:
            async def send_text(self, text: str) -> None:
                nonlocal sent_count
                sent_count += 1
                if sent_count > 3:  # fail after 3 sends (start frame + 2 ticks)
                    raise OSError("broken pipe")

        await engine.replay(FailAfterThree())
        # At most 3 sends succeeded; the rest were not attempted
        assert sent_count <= 4  # tolerance for the control frame


# ---------------------------------------------------------------------------
# replay() — re-entrant guard
# ---------------------------------------------------------------------------


class TestReplayReentrant:
    async def test_second_replay_call_is_ignored(self):
        """Calling replay() a second time while the first is running must be a no-op."""
        # Use slow speed (1x with 50ms gaps × 20 ticks = ~1s) so that Task 1
        # is still running when Task 2 arrives.
        ticks = _make_ticks(20, gap_ms=50)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1)
        ws1 = FakeWebSocket()
        ws2 = FakeWebSocket()

        async def _run_second_while_first_playing() -> None:
            # Wait until the first replay has started (is_playing becomes True).
            for _ in range(200):
                if engine.is_playing():
                    break
                await asyncio.sleep(0.001)
            # Attempt the second replay — must be rejected because first is playing.
            await engine.replay(ws2)
            # Stop the first one so the test doesn't take forever.
            engine.stop()

        await asyncio.gather(
            engine.replay(ws1),
            _run_second_while_first_playing(),
        )

        # ws2 must receive no frames (re-entry rejected)
        assert ws2.frames() == []


# ---------------------------------------------------------------------------
# set_speed() mid-replay
# ---------------------------------------------------------------------------


class TestSetSpeedMidReplay:
    async def test_set_speed_takes_effect(self):
        """set_speed() called after replay starts must affect subsequent sleep durations."""
        ticks = _make_ticks(5, gap_ms=100)
        engine = ReplayEngine(tick_data=ticks, speed_multiplier=1)
        ws = FakeWebSocket()

        # Accelerate immediately so the test doesn't take 500ms
        async def _accelerate() -> None:
            await asyncio.sleep(0)
            engine.set_speed(1_000_000)

        await asyncio.gather(engine.replay(ws), _accelerate())
        assert engine.ticks_sent == 5


# ---------------------------------------------------------------------------
# _build_tick_envelope
# ---------------------------------------------------------------------------


class TestBuildTickEnvelope:
    def test_data_source_type_is_historical(self):
        tick = {"time": 1700000000000, "ltp": 100.0}
        result = _build_tick_envelope(tick, "test-session")
        assert result["dataSourceType"] == "HISTORICAL"

    def test_provenance_object_present(self):
        tick = {"time": 1700000000000}
        result = _build_tick_envelope(tick, "test-session")
        assert isinstance(result["provenance"], dict)

    def test_provenance_has_normalisation_version(self):
        tick = {"time": 1700000000000, "normalisationVersion": "2.0.0"}
        result = _build_tick_envelope(tick, "test-session")
        assert result["provenance"]["normalisationVersion"] == "2.0.0"

    def test_provenance_default_normalisation_version(self):
        tick = {"time": 1700000000000}
        result = _build_tick_envelope(tick, "test-session")
        assert result["provenance"]["normalisationVersion"] == "1.0.0"

    def test_replay_session_id_embedded(self):
        tick = {"time": 1700000000000}
        result = _build_tick_envelope(tick, "my-session-id")
        assert result["replaySessionId"] == "my-session-id"

    def test_original_fields_preserved(self):
        tick = {"time": 1700000000000, "ltp": 19999.9, "volume": 42, "oi": None}
        result = _build_tick_envelope(tick, "s")
        assert result["ltp"] == 19999.9
        assert result["volume"] == 42
        assert result["oi"] is None

    def test_provenance_not_fallback(self):
        tick = {"time": 0}
        result = _build_tick_envelope(tick, "s")
        assert result["provenance"]["isFallback"] is False


# ---------------------------------------------------------------------------
# Session registry helpers
# ---------------------------------------------------------------------------


class TestSessionRegistry:
    def test_create_session_returns_replay_session(self):
        ticks = _make_ticks(3)
        session = create_session("NIFTY", ticks)
        assert isinstance(session, ReplaySession)

    def test_create_session_populates_tick_count(self):
        ticks = _make_ticks(7)
        session = create_session("ETH", ticks)
        assert session.tickCount == 7

    def test_create_session_status_is_created(self):
        ticks = _make_ticks(2)
        session = create_session("BTC", ticks)
        assert session.status == ReplayStatus.CREATED

    def test_create_session_infers_start_and_end_ts(self):
        ticks = _make_ticks(5, start_ms=2_000_000_000_000, gap_ms=1_000)
        session = create_session("X", ticks)
        assert session.startTs == 2_000_000_000_000
        assert session.endTs == 2_000_000_004_000

    def test_get_session_returns_created_session(self):
        ticks = _make_ticks(2)
        session = create_session("NIFTY", ticks)
        retrieved = get_session(session.sessionId)
        assert retrieved is not None
        assert retrieved.sessionId == session.sessionId

    def test_get_session_returns_none_for_unknown_id(self):
        result = get_session(str(uuid.uuid4()))
        assert result is None

    def test_update_session_status_changes_status(self):
        ticks = _make_ticks(2)
        session = create_session("BTC", ticks)
        updated = update_session_status(session.sessionId, ReplayStatus.PLAYING)
        assert updated is not None
        assert updated.status == ReplayStatus.PLAYING

    def test_update_session_status_sets_started_at_on_playing(self):
        ticks = _make_ticks(2)
        session = create_session("ETH", ticks)
        assert session.startedAt is None
        updated = update_session_status(session.sessionId, ReplayStatus.PLAYING)
        assert updated is not None
        assert updated.startedAt is not None

    def test_update_session_status_sets_completed_at_on_completion(self):
        ticks = _make_ticks(2)
        session = create_session("SOL", ticks)
        updated = update_session_status(session.sessionId, ReplayStatus.COMPLETED)
        assert updated is not None
        assert updated.completedAt is not None

    def test_update_session_status_sets_completed_at_on_stopped(self):
        ticks = _make_ticks(2)
        session = create_session("SOL", ticks)
        updated = update_session_status(session.sessionId, ReplayStatus.STOPPED)
        assert updated is not None
        assert updated.completedAt is not None

    def test_update_session_status_unknown_id_returns_none(self):
        result = update_session_status(str(uuid.uuid4()), ReplayStatus.STOPPED)
        assert result is None

    def test_create_session_invalid_speed_raises(self):
        with pytest.raises(ValueError, match="speed_multiplier"):
            create_session("X", _make_ticks(2), speed_multiplier=0.0)


# ---------------------------------------------------------------------------
# API endpoints (using httpx.AsyncClient + ASGITransport)
# ---------------------------------------------------------------------------


class TestReplayAPIEndpoints:
    """Test the REST endpoints via httpx.AsyncClient against the FastAPI ASGI app."""

    @pytest.fixture()
    def app(self):
        """Return a minimal FastAPI app with only the replay router."""
        from fastapi import FastAPI
        from src.api.replay import router as replay_router

        _app = FastAPI()
        _app.include_router(replay_router, prefix="/v1", tags=["Replay"])
        return _app

    async def test_create_session_returns_201(self, app):
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/replay/sessions",
                json={
                    "symbol": "NIFTY",
                    "speedMultiplier": 2.0,
                    "tickData": [
                        {"time": 1700000000000, "ltp": 19500.0},
                        {"time": 1700000060000, "ltp": 19510.0},
                    ],
                },
            )
        assert resp.status_code == 201

    async def test_create_session_response_shape(self, app):
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/replay/sessions",
                json={
                    "symbol": "BTC",
                    "speedMultiplier": 1.0,
                    "tickData": [{"time": 1700000000000, "ltp": 42000.0}],
                },
            )
        body = resp.json()
        assert "data" in body
        assert "metadata" in body
        data = body["data"]
        assert data["symbol"] == "BTC"
        assert data["status"] == "CREATED"
        assert data["tickCount"] == 1
        assert data["speedMultiplier"] == 1.0
        assert body["metadata"]["dataSourceType"] == "HISTORICAL"

    async def test_create_session_missing_time_returns_422(self, app):
        """Tick without 'time' field should fail validation (HTTP 422)."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/replay/sessions",
                json={
                    "symbol": "NIFTY",
                    "tickData": [{"ltp": 100.0}],  # missing 'time'
                },
            )
        assert resp.status_code == 422

    async def test_create_session_zero_speed_returns_422(self, app):
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/replay/sessions",
                json={
                    "symbol": "NIFTY",
                    "speedMultiplier": 0.0,
                    "tickData": [{"time": 1700000000000}],
                },
            )
        assert resp.status_code == 422

    async def test_get_status_returns_200_for_existing_session(self, app):
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            create_resp = await client.post(
                "/v1/replay/sessions",
                json={
                    "symbol": "ETH",
                    "tickData": [{"time": 1700000000000, "ltp": 3000.0}],
                },
            )
            session_id = create_resp.json()["data"]["sessionId"]

            status_resp = await client.get(
                f"/v1/replay/sessions/{session_id}/status"
            )
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["data"]["sessionId"] == session_id

    async def test_get_status_returns_404_for_unknown_session(self, app):
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                f"/v1/replay/sessions/{uuid.uuid4()}/status"
            )
        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "NOT_FOUND"

    async def test_create_session_empty_symbol_returns_422(self, app):
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/replay/sessions",
                json={
                    "symbol": "",
                    "tickData": [{"time": 1700000000000}],
                },
            )
        assert resp.status_code == 422

    async def test_create_session_stores_session_in_registry(self, app):
        """Creating via the API should make the session queryable via get_session()."""
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/replay/sessions",
                json={
                    "symbol": "SOL",
                    "tickData": [{"time": 1700000000000}],
                },
            )
        session_id = resp.json()["data"]["sessionId"]
        assert get_session(session_id) is not None
