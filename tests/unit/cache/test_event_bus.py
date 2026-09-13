"""
tests/unit/cache/test_event_bus.py

Unit tests for src/cache/event_bus.py — EventBus.

Requirements: 15.1, 15.4

Coverage:
  EventType
    - All ten EventType values are present with correct string values
    - EventType is a string enum (subscriptable by value)

  StreamKey
    - All nine stream key constants are defined
    - Each key starts with ``mds:events:``

  STREAM_MAXLEN
    - All nine stream keys have an entry in STREAM_MAXLEN
    - incidents stream has the highest MAXLEN (7-day retention)
    - All MAXLEN values are positive integers

  ALL_STREAM_KEYS
    - Frozenset contains exactly 9 keys

  EventBus.publish
    - Calls XADD with correct stream_key, event_type field, and payload JSON
    - XADD called with MAXLEN from STREAM_MAXLEN for the given stream
    - Uses approximate=True for XADD trimming
    - Returns message ID string from XADD
    - Decodes bytes message ID to str
    - Falls back gracefully on RedisError (returns "")
    - Logs structured warning on RedisError

  EventBus.publish_tick
    - Calls publish() targeting mds:events:ticks with QUOTE_UPDATED event type
    - Merges symbol into payload
    - Returns message ID from underlying publish

  EventBus.publish_dataset_ready
    - Calls publish() targeting mds:events:dataset-ready with DATASET_READY event type
    - Passes event_data dict to publish

  EventBus.publish_incident
    - Calls publish() targeting mds:events:incidents with DATA_INCIDENT event type
    - Passes incident dict to publish

  EventBus.publish_quality_event
    - Calls publish() targeting mds:events:quality
    - Passes through event_type (QUALITY_DEGRADED / QUALITY_RESTORED)

  EventBus.publish_provider_switch
    - Calls publish() targeting mds:events:provider-switch with PROVIDER_SWITCH event type

  EventBus.publish_reconnect
    - Calls publish() targeting mds:events:reconnect with RECONNECT event type
    - Payload includes type="reconnect", provider, and attempt number

  EventBus.publish_connection_failed
    - Calls publish() targeting mds:events:connection-failed with CONNECTION_FAILED event type
    - Payload includes type="connection_failed" and provider

  EventBus.get_stream_info
    - Returns dict with required keys: stream_key, length, first_entry_id,
      last_entry_id, radix_tree_keys, groups
    - Returns stream_key in result for reference
    - On RedisError returns fallback dict with length=0 and error key
    - Does not raise on RedisError

  EventBus.read
    - Returns list of decoded message dicts with id, event_type, payload keys
    - Decodes payload JSON string back to dict
    - Returns empty list on RedisError
    - Returns empty list when no messages available (XREAD returns None/empty)
    - Passes block_ms to XREAD when provided
    - Omits block kwarg when block_ms is None
    - Accepts last_id parameter (passed through to XREAD)
    - Accepts count parameter (passed through to XREAD)
    - Handles bytes keys/values from Redis (decode_responses=False scenario)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError, TimeoutError as RedisTimeoutError

from src.cache.event_bus import (
    ALL_STREAM_KEYS,
    STREAM_MAXLEN,
    EventBus,
    EventType,
    StreamKey,
    _24H_GENERAL_MAXLEN,
    _7D_INCIDENTS_MAXLEN,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _make_bus(
    xadd_return: str | bytes = "1705300000123-0",
    xadd_raises: Exception | None = None,
    xinfo_return: dict | None = None,
    xinfo_raises: Exception | None = None,
    xread_return: list | None = None,
    xread_raises: Exception | None = None,
) -> tuple[EventBus, MagicMock]:
    """Build an EventBus with a fully mocked Redis client."""
    mock_redis = MagicMock()

    # xadd
    if xadd_raises:
        mock_redis.xadd = AsyncMock(side_effect=xadd_raises)
    else:
        mock_redis.xadd = AsyncMock(return_value=xadd_return)

    # xinfo_stream
    if xinfo_raises:
        mock_redis.xinfo_stream = AsyncMock(side_effect=xinfo_raises)
    else:
        if xinfo_return is None:
            xinfo_return = {
                "length": 42,
                "first-entry": ["1705300000000-0", {}],
                "last-entry": ["1705300000123-0", {}],
                "radix-tree-keys": 3,
                "groups": 1,
            }
        mock_redis.xinfo_stream = AsyncMock(return_value=xinfo_return)

    # xread
    if xread_raises:
        mock_redis.xread = AsyncMock(side_effect=xread_raises)
    else:
        mock_redis.xread = AsyncMock(return_value=xread_return)

    return EventBus(mock_redis), mock_redis


# ===========================================================================
# EventType
# ===========================================================================


class TestEventType:
    """Verify all EventType members and their string values."""

    def test_quote_updated(self) -> None:
        assert EventType.QUOTE_UPDATED == "QuoteUpdated"

    def test_candle_closed(self) -> None:
        assert EventType.CANDLE_CLOSED == "CandleClosed"

    def test_option_chain_updated(self) -> None:
        assert EventType.OPTION_CHAIN_UPDATED == "OptionChainUpdated"

    def test_dataset_ready(self) -> None:
        assert EventType.DATASET_READY == "DatasetReady"

    def test_data_incident(self) -> None:
        assert EventType.DATA_INCIDENT == "DataIncident"

    def test_quality_degraded(self) -> None:
        assert EventType.QUALITY_DEGRADED == "QualityDegraded"

    def test_quality_restored(self) -> None:
        assert EventType.QUALITY_RESTORED == "QualityRestored"

    def test_circuit_open(self) -> None:
        assert EventType.CIRCUIT_OPEN == "CircuitOpen"

    def test_provider_switch(self) -> None:
        assert EventType.PROVIDER_SWITCH == "ProviderSwitch"

    def test_connection_failed(self) -> None:
        assert EventType.CONNECTION_FAILED == "ConnectionFailed"

    def test_is_string_enum(self) -> None:
        """EventType values can be compared directly to strings."""
        assert EventType.QUOTE_UPDATED == "QuoteUpdated"
        assert isinstance(EventType.QUOTE_UPDATED, str)

    def test_all_ten_primary_event_types_exist(self) -> None:
        primary_types = {
            "QuoteUpdated", "CandleClosed", "OptionChainUpdated", "DatasetReady",
            "DataIncident", "QualityDegraded", "QualityRestored", "CircuitOpen",
            "ProviderSwitch", "ConnectionFailed",
        }
        actual = {e.value for e in EventType if e != EventType.RECONNECT}
        assert actual == primary_types


# ===========================================================================
# StreamKey
# ===========================================================================


class TestStreamKey:
    """Verify all nine stream key constants."""

    def test_ticks_key(self) -> None:
        assert StreamKey.TICKS == "mds:events:ticks"

    def test_candles_key(self) -> None:
        assert StreamKey.CANDLES == "mds:events:candles"

    def test_option_chain_key(self) -> None:
        assert StreamKey.OPTION_CHAIN == "mds:events:option-chain"

    def test_dataset_ready_key(self) -> None:
        assert StreamKey.DATASET_READY == "mds:events:dataset-ready"

    def test_incidents_key(self) -> None:
        assert StreamKey.INCIDENTS == "mds:events:incidents"

    def test_quality_key(self) -> None:
        assert StreamKey.QUALITY == "mds:events:quality"

    def test_provider_switch_key(self) -> None:
        assert StreamKey.PROVIDER_SWITCH == "mds:events:provider-switch"

    def test_reconnect_key(self) -> None:
        assert StreamKey.RECONNECT == "mds:events:reconnect"

    def test_connection_failed_key(self) -> None:
        assert StreamKey.CONNECTION_FAILED == "mds:events:connection-failed"

    def test_all_keys_start_with_mds_events(self) -> None:
        for attr in ("TICKS", "CANDLES", "OPTION_CHAIN", "DATASET_READY", "INCIDENTS",
                     "QUALITY", "PROVIDER_SWITCH", "RECONNECT", "CONNECTION_FAILED"):
            val: str = getattr(StreamKey, attr)
            assert val.startswith("mds:events:"), f"{attr} = {val!r} must start with 'mds:events:'"


# ===========================================================================
# STREAM_MAXLEN & ALL_STREAM_KEYS
# ===========================================================================


class TestStreamMaxlen:
    """Verify MAXLEN configuration for all streams."""

    def test_all_nine_stream_keys_have_maxlen(self) -> None:
        expected_keys = {
            StreamKey.TICKS, StreamKey.CANDLES, StreamKey.OPTION_CHAIN,
            StreamKey.DATASET_READY, StreamKey.INCIDENTS, StreamKey.QUALITY,
            StreamKey.PROVIDER_SWITCH, StreamKey.RECONNECT, StreamKey.CONNECTION_FAILED,
        }
        assert set(STREAM_MAXLEN.keys()) == expected_keys

    def test_all_maxlen_values_are_positive(self) -> None:
        for key, maxlen in STREAM_MAXLEN.items():
            assert maxlen > 0, f"{key} MAXLEN must be positive, got {maxlen}"

    def test_incidents_has_highest_maxlen(self) -> None:
        """7-day incidents stream should have a larger (or equal) MAXLEN than 24h streams."""
        assert STREAM_MAXLEN[StreamKey.INCIDENTS] >= _24H_GENERAL_MAXLEN

    def test_incidents_maxlen_equals_7d_constant(self) -> None:
        assert STREAM_MAXLEN[StreamKey.INCIDENTS] == _7D_INCIDENTS_MAXLEN

    def test_all_stream_keys_frozenset_has_nine_elements(self) -> None:
        assert len(ALL_STREAM_KEYS) == 9

    def test_all_stream_keys_matches_maxlen_keys(self) -> None:
        assert ALL_STREAM_KEYS == frozenset(STREAM_MAXLEN.keys())


# ===========================================================================
# EventBus.publish — core
# ===========================================================================


class TestEventBusPublish:
    """Tests for the core publish() method."""

    @pytest.mark.asyncio
    async def test_returns_message_id_string(self) -> None:
        bus, _ = _make_bus(xadd_return="1705300000123-0")
        msg_id = await bus.publish(StreamKey.TICKS, EventType.QUOTE_UPDATED, {"ltp": 22150.5})
        assert msg_id == "1705300000123-0"

    @pytest.mark.asyncio
    async def test_decodes_bytes_message_id(self) -> None:
        bus, _ = _make_bus(xadd_return=b"1705300000999-1")
        msg_id = await bus.publish(StreamKey.TICKS, EventType.QUOTE_UPDATED, {})
        assert isinstance(msg_id, str)
        assert msg_id == "1705300000999-1"

    @pytest.mark.asyncio
    async def test_xadd_called_with_correct_stream_key(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish(StreamKey.CANDLES, EventType.CANDLE_CLOSED, {})
        call_args = mock_redis.xadd.call_args
        assert call_args[0][0] == StreamKey.CANDLES

    @pytest.mark.asyncio
    async def test_xadd_message_contains_event_type_field(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish(StreamKey.TICKS, EventType.QUOTE_UPDATED, {"ltp": 100})
        call_args = mock_redis.xadd.call_args
        fields: dict = call_args[0][1]
        assert fields["event_type"] == EventType.QUOTE_UPDATED.value

    @pytest.mark.asyncio
    async def test_xadd_message_contains_json_payload(self) -> None:
        payload = {"instrumentId": "NSE:NIFTY:IDX", "ltp": 22150.5, "volume": 1000}
        bus, mock_redis = _make_bus()
        await bus.publish(StreamKey.TICKS, EventType.QUOTE_UPDATED, payload)
        call_args = mock_redis.xadd.call_args
        fields: dict = call_args[0][1]
        decoded = json.loads(fields["payload"])
        assert decoded == payload

    @pytest.mark.asyncio
    async def test_xadd_called_with_maxlen_for_stream(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish(StreamKey.INCIDENTS, EventType.DATA_INCIDENT, {})
        call_kwargs = mock_redis.xadd.call_args[1]
        assert call_kwargs["maxlen"] == STREAM_MAXLEN[StreamKey.INCIDENTS]

    @pytest.mark.asyncio
    async def test_xadd_uses_approximate_trim(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish(StreamKey.TICKS, EventType.QUOTE_UPDATED, {})
        call_kwargs = mock_redis.xadd.call_args[1]
        assert call_kwargs["approximate"] is True

    @pytest.mark.asyncio
    async def test_returns_empty_string_on_redis_error(self) -> None:
        bus, _ = _make_bus(xadd_raises=RedisConnectionError("down"))
        msg_id = await bus.publish(StreamKey.TICKS, EventType.QUOTE_UPDATED, {})
        assert msg_id == ""

    @pytest.mark.asyncio
    async def test_does_not_raise_on_redis_timeout(self) -> None:
        bus, _ = _make_bus(xadd_raises=RedisTimeoutError("timeout"))
        # Should complete without raising
        result = await bus.publish(StreamKey.TICKS, EventType.QUOTE_UPDATED, {})
        assert result == ""

    @pytest.mark.asyncio
    async def test_logs_warning_on_redis_error(self, caplog: Any) -> None:
        import logging
        bus, _ = _make_bus(xadd_raises=RedisConnectionError("down"))
        with caplog.at_level(logging.WARNING, logger="src.cache.event_bus"):
            await bus.publish(StreamKey.TICKS, EventType.QUOTE_UPDATED, {})
        assert any("stream_publish_failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unknown_stream_key_uses_default_maxlen(self) -> None:
        """An unrecognised stream key falls back to _24H_GENERAL_MAXLEN."""
        bus, mock_redis = _make_bus()
        await bus.publish("mds:events:unknown-future-stream", EventType.QUOTE_UPDATED, {})
        call_kwargs = mock_redis.xadd.call_args[1]
        assert call_kwargs["maxlen"] == _24H_GENERAL_MAXLEN

    @pytest.mark.asyncio
    async def test_payload_with_non_serialisable_value_uses_str_default(self) -> None:
        """Non-JSON-serialisable values in payload are stringified via default=str."""
        from datetime import datetime
        bus, mock_redis = _make_bus()
        now = datetime(2026, 1, 15, 9, 15, 0)
        await bus.publish(StreamKey.QUALITY, EventType.QUALITY_DEGRADED, {"ts": now})
        call_args = mock_redis.xadd.call_args
        fields: dict = call_args[0][1]
        payload = json.loads(fields["payload"])
        # datetime is stringified
        assert "ts" in payload
        assert isinstance(payload["ts"], str)


# ===========================================================================
# EventBus.publish_tick
# ===========================================================================


class TestEventBusPublishTick:
    """Tests for the publish_tick() shortcut."""

    @pytest.mark.asyncio
    async def test_targets_ticks_stream(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_tick("NIFTY", {"ltp": 22150.5})
        call_args = mock_redis.xadd.call_args
        assert call_args[0][0] == StreamKey.TICKS

    @pytest.mark.asyncio
    async def test_uses_quote_updated_event_type(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_tick("NIFTY", {"ltp": 22150.5})
        call_args = mock_redis.xadd.call_args
        fields: dict = call_args[0][1]
        assert fields["event_type"] == EventType.QUOTE_UPDATED.value

    @pytest.mark.asyncio
    async def test_merges_symbol_into_payload(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_tick("NIFTY", {"ltp": 22150.5, "volume": 1000})
        call_args = mock_redis.xadd.call_args
        fields: dict = call_args[0][1]
        payload = json.loads(fields["payload"])
        assert payload["symbol"] == "NIFTY"
        assert payload["ltp"] == 22150.5

    @pytest.mark.asyncio
    async def test_returns_message_id(self) -> None:
        bus, _ = _make_bus(xadd_return="1705300000100-0")
        msg_id = await bus.publish_tick("BANKNIFTY", {})
        assert msg_id == "1705300000100-0"

    @pytest.mark.asyncio
    async def test_returns_empty_string_on_redis_error(self) -> None:
        bus, _ = _make_bus(xadd_raises=RedisConnectionError("down"))
        msg_id = await bus.publish_tick("NIFTY", {"ltp": 100})
        assert msg_id == ""


# ===========================================================================
# EventBus.publish_dataset_ready
# ===========================================================================


class TestEventBusPublishDatasetReady:
    """Tests for the publish_dataset_ready() shortcut."""

    @pytest.mark.asyncio
    async def test_targets_dataset_ready_stream(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_dataset_ready({"dataType": "HISTORICAL_OHLCV"})
        assert mock_redis.xadd.call_args[0][0] == StreamKey.DATASET_READY

    @pytest.mark.asyncio
    async def test_uses_dataset_ready_event_type(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_dataset_ready({})
        fields: dict = mock_redis.xadd.call_args[0][1]
        assert fields["event_type"] == EventType.DATASET_READY.value

    @pytest.mark.asyncio
    async def test_passes_event_data_as_payload(self) -> None:
        event_data = {
            "dataType": "HISTORICAL_OHLCV",
            "instrumentId": "NSE:RELIANCE:EQ",
            "exchange": "NSE",
            "intervalStr": "1m",
            "fromTs": 1705300000000,
            "toTs": 1705386400000,
            "rowCount": 375,
            "provider": "angel_one",
        }
        bus, mock_redis = _make_bus()
        await bus.publish_dataset_ready(event_data)
        fields: dict = mock_redis.xadd.call_args[0][1]
        payload = json.loads(fields["payload"])
        assert payload == event_data

    @pytest.mark.asyncio
    async def test_returns_empty_string_on_redis_error(self) -> None:
        bus, _ = _make_bus(xadd_raises=RedisConnectionError("down"))
        msg_id = await bus.publish_dataset_ready({})
        assert msg_id == ""


# ===========================================================================
# EventBus.publish_incident
# ===========================================================================


class TestEventBusPublishIncident:
    """Tests for the publish_incident() shortcut."""

    @pytest.mark.asyncio
    async def test_targets_incidents_stream(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_incident({"incidentId": "uuid-1"})
        assert mock_redis.xadd.call_args[0][0] == StreamKey.INCIDENTS

    @pytest.mark.asyncio
    async def test_uses_data_incident_event_type(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_incident({})
        fields: dict = mock_redis.xadd.call_args[0][1]
        assert fields["event_type"] == EventType.DATA_INCIDENT.value

    @pytest.mark.asyncio
    async def test_passes_incident_dict_as_payload(self) -> None:
        incident = {
            "incidentId": "abc-123",
            "incidentType": "OHLC_INVARIANT",
            "instrumentId": "NSE:NIFTY:IDX",
            "provider": "angel_one",
            "severity": "HIGH",
        }
        bus, mock_redis = _make_bus()
        await bus.publish_incident(incident)
        fields: dict = mock_redis.xadd.call_args[0][1]
        payload = json.loads(fields["payload"])
        assert payload == incident

    @pytest.mark.asyncio
    async def test_incidents_maxlen_applied(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_incident({})
        call_kwargs = mock_redis.xadd.call_args[1]
        assert call_kwargs["maxlen"] == _7D_INCIDENTS_MAXLEN

    @pytest.mark.asyncio
    async def test_returns_empty_string_on_redis_error(self) -> None:
        bus, _ = _make_bus(xadd_raises=RedisConnectionError("down"))
        msg_id = await bus.publish_incident({"incidentId": "x"})
        assert msg_id == ""


# ===========================================================================
# EventBus.publish_quality_event
# ===========================================================================


class TestEventBusPublishQualityEvent:
    """Tests for the publish_quality_event() shortcut."""

    @pytest.mark.asyncio
    async def test_targets_quality_stream(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_quality_event(EventType.QUALITY_DEGRADED, {"score": 45})
        assert mock_redis.xadd.call_args[0][0] == StreamKey.QUALITY

    @pytest.mark.asyncio
    async def test_passes_quality_degraded_event_type(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_quality_event(EventType.QUALITY_DEGRADED, {})
        fields: dict = mock_redis.xadd.call_args[0][1]
        assert fields["event_type"] == EventType.QUALITY_DEGRADED.value

    @pytest.mark.asyncio
    async def test_passes_quality_restored_event_type(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_quality_event(EventType.QUALITY_RESTORED, {})
        fields: dict = mock_redis.xadd.call_args[0][1]
        assert fields["event_type"] == EventType.QUALITY_RESTORED.value

    @pytest.mark.asyncio
    async def test_passes_data_dict_as_payload(self) -> None:
        data = {"instrumentId": "NSE:NIFTY:IDX", "score": 42, "previousScore": 55}
        bus, mock_redis = _make_bus()
        await bus.publish_quality_event(EventType.QUALITY_DEGRADED, data)
        fields: dict = mock_redis.xadd.call_args[0][1]
        payload = json.loads(fields["payload"])
        assert payload == data

    @pytest.mark.asyncio
    async def test_returns_empty_string_on_redis_error(self) -> None:
        bus, _ = _make_bus(xadd_raises=RedisConnectionError("down"))
        msg_id = await bus.publish_quality_event(EventType.QUALITY_DEGRADED, {})
        assert msg_id == ""


# ===========================================================================
# EventBus.publish_provider_switch
# ===========================================================================


class TestEventBusPublishProviderSwitch:
    """Tests for the publish_provider_switch() shortcut."""

    @pytest.mark.asyncio
    async def test_targets_provider_switch_stream(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_provider_switch({"fromProvider": "angel_one", "toProvider": "openchart"})
        assert mock_redis.xadd.call_args[0][0] == StreamKey.PROVIDER_SWITCH

    @pytest.mark.asyncio
    async def test_uses_provider_switch_event_type(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_provider_switch({})
        fields: dict = mock_redis.xadd.call_args[0][1]
        assert fields["event_type"] == EventType.PROVIDER_SWITCH.value

    @pytest.mark.asyncio
    async def test_passes_switch_data_as_payload(self) -> None:
        switch_data = {
            "fromProvider": "angel_one",
            "toProvider": "openchart",
            "reason": "circuit_breaker_open",
            "dataset": "HISTORICAL_OHLCV",
            "instrumentId": "NSE:RELIANCE:EQ",
            "timestamp": "2026-01-15T09:15:00Z",
        }
        bus, mock_redis = _make_bus()
        await bus.publish_provider_switch(switch_data)
        fields: dict = mock_redis.xadd.call_args[0][1]
        payload = json.loads(fields["payload"])
        assert payload == switch_data


# ===========================================================================
# EventBus.publish_reconnect
# ===========================================================================


class TestEventBusPublishReconnect:
    """Tests for the publish_reconnect() shortcut."""

    @pytest.mark.asyncio
    async def test_targets_reconnect_stream(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_reconnect("angel_one", 1)
        assert mock_redis.xadd.call_args[0][0] == StreamKey.RECONNECT

    @pytest.mark.asyncio
    async def test_payload_contains_type_reconnect(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_reconnect("angel_one", 3)
        fields: dict = mock_redis.xadd.call_args[0][1]
        payload = json.loads(fields["payload"])
        assert payload["type"] == "reconnect"

    @pytest.mark.asyncio
    async def test_payload_contains_provider(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_reconnect("upstox", 2)
        fields: dict = mock_redis.xadd.call_args[0][1]
        payload = json.loads(fields["payload"])
        assert payload["provider"] == "upstox"

    @pytest.mark.asyncio
    async def test_payload_contains_attempt_number(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_reconnect("angel_one", 5)
        fields: dict = mock_redis.xadd.call_args[0][1]
        payload = json.loads(fields["payload"])
        assert payload["attempt"] == 5

    @pytest.mark.asyncio
    async def test_returns_empty_string_on_redis_error(self) -> None:
        bus, _ = _make_bus(xadd_raises=RedisConnectionError("down"))
        msg_id = await bus.publish_reconnect("binance", 1)
        assert msg_id == ""


# ===========================================================================
# EventBus.publish_connection_failed
# ===========================================================================


class TestEventBusPublishConnectionFailed:
    """Tests for the publish_connection_failed() shortcut."""

    @pytest.mark.asyncio
    async def test_targets_connection_failed_stream(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_connection_failed("angel_one")
        assert mock_redis.xadd.call_args[0][0] == StreamKey.CONNECTION_FAILED

    @pytest.mark.asyncio
    async def test_uses_connection_failed_event_type(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_connection_failed("angel_one")
        fields: dict = mock_redis.xadd.call_args[0][1]
        assert fields["event_type"] == EventType.CONNECTION_FAILED.value

    @pytest.mark.asyncio
    async def test_payload_contains_type_connection_failed(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_connection_failed("angel_one")
        fields: dict = mock_redis.xadd.call_args[0][1]
        payload = json.loads(fields["payload"])
        assert payload["type"] == "connection_failed"

    @pytest.mark.asyncio
    async def test_payload_contains_provider(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.publish_connection_failed("upstox")
        fields: dict = mock_redis.xadd.call_args[0][1]
        payload = json.loads(fields["payload"])
        assert payload["provider"] == "upstox"

    @pytest.mark.asyncio
    async def test_returns_empty_string_on_redis_error(self) -> None:
        bus, _ = _make_bus(xadd_raises=RedisConnectionError("down"))
        msg_id = await bus.publish_connection_failed("binance")
        assert msg_id == ""


# ===========================================================================
# EventBus.get_stream_info
# ===========================================================================


class TestEventBusGetStreamInfo:
    """Tests for the get_stream_info() method."""

    @pytest.mark.asyncio
    async def test_returns_dict_with_required_keys(self) -> None:
        bus, _ = _make_bus()
        info = await bus.get_stream_info(StreamKey.TICKS)
        required_keys = {"stream_key", "length", "first_entry_id", "last_entry_id",
                         "radix_tree_keys", "groups"}
        assert required_keys.issubset(info.keys())

    @pytest.mark.asyncio
    async def test_includes_stream_key_in_result(self) -> None:
        bus, _ = _make_bus()
        info = await bus.get_stream_info(StreamKey.TICKS)
        assert info["stream_key"] == StreamKey.TICKS

    @pytest.mark.asyncio
    async def test_returns_length_from_xinfo(self) -> None:
        bus, _ = _make_bus(
            xinfo_return={
                "length": 99,
                "first-entry": ["100-0", {}],
                "last-entry": ["200-0", {}],
                "radix-tree-keys": 1,
                "groups": 0,
            }
        )
        info = await bus.get_stream_info(StreamKey.TICKS)
        assert info["length"] == 99

    @pytest.mark.asyncio
    async def test_calls_xinfo_stream_with_correct_key(self) -> None:
        bus, mock_redis = _make_bus()
        await bus.get_stream_info(StreamKey.INCIDENTS)
        mock_redis.xinfo_stream.assert_awaited_once_with(StreamKey.INCIDENTS)

    @pytest.mark.asyncio
    async def test_returns_fallback_dict_on_redis_error(self) -> None:
        bus, _ = _make_bus(xinfo_raises=RedisConnectionError("down"))
        info = await bus.get_stream_info(StreamKey.TICKS)
        assert info["length"] == 0
        assert "error" in info
        assert info["stream_key"] == StreamKey.TICKS

    @pytest.mark.asyncio
    async def test_does_not_raise_on_redis_error(self) -> None:
        bus, _ = _make_bus(xinfo_raises=RedisTimeoutError("timeout"))
        # Should not raise
        info = await bus.get_stream_info(StreamKey.QUALITY)
        assert isinstance(info, dict)

    @pytest.mark.asyncio
    async def test_handles_bytes_keys_from_redis(self) -> None:
        """When decode_responses=False, XINFO STREAM returns bytes keys."""
        bus, _ = _make_bus(
            xinfo_return={
                b"length": 7,
                b"first-entry": [b"100-0", {}],
                b"last-entry": [b"200-0", {}],
                b"radix-tree-keys": 2,
                b"groups": 1,
            }
        )
        info = await bus.get_stream_info(StreamKey.TICKS)
        assert info["length"] == 7


# ===========================================================================
# EventBus.read
# ===========================================================================


def _make_xread_result(
    stream_key: str,
    entries: list[tuple[str, dict[str, str]]],
) -> list[list[Any]]:
    """Build a fake XREAD return value."""
    return [[stream_key, entries]]


class TestEventBusRead:
    """Tests for the read() method."""

    @pytest.mark.asyncio
    async def test_returns_list_of_dicts(self) -> None:
        entries = [
            ("1705300000123-0", {"event_type": "QuoteUpdated", "payload": '{"ltp": 100}'}),
        ]
        bus, _ = _make_bus(xread_return=_make_xread_result(StreamKey.TICKS, entries))
        messages = await bus.read(StreamKey.TICKS)
        assert isinstance(messages, list)
        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_each_message_has_required_keys(self) -> None:
        entries = [
            ("1705300000123-0", {"event_type": "QuoteUpdated", "payload": '{"ltp": 100}'}),
        ]
        bus, _ = _make_bus(xread_return=_make_xread_result(StreamKey.TICKS, entries))
        messages = await bus.read(StreamKey.TICKS)
        msg = messages[0]
        assert "id" in msg
        assert "event_type" in msg
        assert "payload" in msg

    @pytest.mark.asyncio
    async def test_decodes_payload_json_to_dict(self) -> None:
        payload_dict = {"ltp": 22150.5, "symbol": "NIFTY"}
        entries = [
            ("1705300000123-0", {"event_type": "QuoteUpdated",
                                  "payload": json.dumps(payload_dict)}),
        ]
        bus, _ = _make_bus(xread_return=_make_xread_result(StreamKey.TICKS, entries))
        messages = await bus.read(StreamKey.TICKS)
        assert messages[0]["payload"] == payload_dict

    @pytest.mark.asyncio
    async def test_returns_message_id(self) -> None:
        entries = [
            ("1705300000123-0", {"event_type": "CandleClosed", "payload": "{}"}),
        ]
        bus, _ = _make_bus(xread_return=_make_xread_result(StreamKey.CANDLES, entries))
        messages = await bus.read(StreamKey.CANDLES)
        assert messages[0]["id"] == "1705300000123-0"

    @pytest.mark.asyncio
    async def test_returns_event_type_string(self) -> None:
        entries = [
            ("1705300000123-0", {"event_type": "DataIncident", "payload": "{}"}),
        ]
        bus, _ = _make_bus(xread_return=_make_xread_result(StreamKey.INCIDENTS, entries))
        messages = await bus.read(StreamKey.INCIDENTS)
        assert messages[0]["event_type"] == "DataIncident"

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_messages(self) -> None:
        bus, _ = _make_bus(xread_return=None)
        messages = await bus.read(StreamKey.TICKS)
        assert messages == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_redis_error(self) -> None:
        bus, _ = _make_bus(xread_raises=RedisConnectionError("down"))
        messages = await bus.read(StreamKey.TICKS)
        assert messages == []

    @pytest.mark.asyncio
    async def test_does_not_raise_on_redis_error(self) -> None:
        bus, _ = _make_bus(xread_raises=RedisTimeoutError("timeout"))
        # Should not raise
        messages = await bus.read(StreamKey.TICKS)
        assert isinstance(messages, list)

    @pytest.mark.asyncio
    async def test_passes_block_ms_when_provided(self) -> None:
        bus, mock_redis = _make_bus(xread_return=None)
        await bus.read(StreamKey.TICKS, block_ms=5000)
        call_kwargs = mock_redis.xread.call_args[1]
        assert call_kwargs.get("block") == 5000

    @pytest.mark.asyncio
    async def test_no_block_kwarg_when_block_ms_is_none(self) -> None:
        bus, mock_redis = _make_bus(xread_return=None)
        await bus.read(StreamKey.TICKS, block_ms=None)
        call_kwargs = mock_redis.xread.call_args[1]
        assert "block" not in call_kwargs

    @pytest.mark.asyncio
    async def test_passes_count_to_xread(self) -> None:
        bus, mock_redis = _make_bus(xread_return=None)
        await bus.read(StreamKey.TICKS, count=50)
        call_kwargs = mock_redis.xread.call_args[1]
        assert call_kwargs.get("count") == 50

    @pytest.mark.asyncio
    async def test_passes_last_id_to_xread(self) -> None:
        bus, mock_redis = _make_bus(xread_return=None)
        await bus.read(StreamKey.TICKS, last_id="1705300000100-0")
        call_kwargs = mock_redis.xread.call_args[1]
        streams: dict = call_kwargs["streams"]
        assert streams[StreamKey.TICKS] == "1705300000100-0"

    @pytest.mark.asyncio
    async def test_default_last_id_is_dollar(self) -> None:
        bus, mock_redis = _make_bus(xread_return=None)
        await bus.read(StreamKey.TICKS)
        call_kwargs = mock_redis.xread.call_args[1]
        streams: dict = call_kwargs["streams"]
        assert streams[StreamKey.TICKS] == "$"

    @pytest.mark.asyncio
    async def test_handles_multiple_messages(self) -> None:
        entries = [
            ("1705300000100-0", {"event_type": "QuoteUpdated", "payload": '{"ltp": 100}'}),
            ("1705300000200-0", {"event_type": "QuoteUpdated", "payload": '{"ltp": 101}'}),
            ("1705300000300-0", {"event_type": "QuoteUpdated", "payload": '{"ltp": 102}'}),
        ]
        bus, _ = _make_bus(xread_return=_make_xread_result(StreamKey.TICKS, entries))
        messages = await bus.read(StreamKey.TICKS)
        assert len(messages) == 3

    @pytest.mark.asyncio
    async def test_handles_bytes_entry_id_and_fields(self) -> None:
        """When decode_responses=False, entry IDs and field keys/values are bytes."""
        entries = [
            (b"1705300000123-0", {b"event_type": b"QuoteUpdated",
                                   b"payload": b'{"ltp": 100}'}),
        ]
        bus, _ = _make_bus(xread_return=_make_xread_result(StreamKey.TICKS, entries))
        messages = await bus.read(StreamKey.TICKS)
        assert messages[0]["id"] == "1705300000123-0"
        assert messages[0]["event_type"] == "QuoteUpdated"
        assert messages[0]["payload"] == {"ltp": 100}

    @pytest.mark.asyncio
    async def test_handles_malformed_json_payload_gracefully(self) -> None:
        """A malformed JSON payload should not crash the consumer."""
        entries = [
            ("1705300000123-0", {"event_type": "DataIncident", "payload": "NOT JSON!"}),
        ]
        bus, _ = _make_bus(xread_return=_make_xread_result(StreamKey.INCIDENTS, entries))
        messages = await bus.read(StreamKey.INCIDENTS)
        # Should still return the message with an empty payload dict
        assert len(messages) == 1
        assert messages[0]["payload"] == {}
