"""
Unit tests for src/providers/gateway.py — task 4.4.

Covers:
- ``log_provider_switch`` emits a structured log entry with all required fields
- Every ``SwitchReason`` value is accepted without error
- The ``timestamp`` field is UTC ISO-8601 with millisecond precision and Z suffix
- ``get_provider_health`` returns a dict covering every ``ProviderId``
- ``fetch`` stub raises ``NotImplementedError``

Requirements: 5.10
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.core.schemas.provider import DataType, ProviderId
from src.providers.gateway import ProviderGateway, SwitchReason


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Strict UTC ISO-8601 with milliseconds: 2026-01-15T09:15:00.000Z
_UTC_ISO8601_MS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


def _call_log_switch(
    from_provider: ProviderId = ProviderId.ANGEL_ONE,
    to_provider: ProviderId = ProviderId.OPENCHART,
    reason: SwitchReason = SwitchReason.CIRCUIT_BREAKER_OPEN,
    dataset: DataType = DataType.HISTORICAL_OHLCV,
    instrument_id: str = "NSE:RELIANCE:EQ",
) -> dict[str, Any]:
    """Call ``log_provider_switch`` and capture the structlog event dict."""
    captured: list[dict[str, Any]] = []

    def _fake_info(event: str, **kw: Any) -> None:  # noqa: ANN001
        captured.append({"event": event, **kw})

    mock_logger = MagicMock()
    mock_logger.info.side_effect = _fake_info

    with patch("src.providers.gateway.logger", mock_logger):
        ProviderGateway.log_provider_switch(
            from_provider=from_provider,
            to_provider=to_provider,
            reason=reason,
            dataset=dataset,
            instrument_id=instrument_id,
        )

    assert len(captured) == 1, "Expected exactly one log entry"
    return captured[0]


# ---------------------------------------------------------------------------
# log_provider_switch — required field presence
# ---------------------------------------------------------------------------


class TestLogProviderSwitch:
    def test_event_name_is_provider_switch(self) -> None:
        entry = _call_log_switch()
        assert entry["event"] == "provider_switch"

    def test_from_provider_field(self) -> None:
        entry = _call_log_switch(from_provider=ProviderId.ANGEL_ONE)
        assert entry["fromProvider"] == ProviderId.ANGEL_ONE.value

    def test_to_provider_field(self) -> None:
        entry = _call_log_switch(to_provider=ProviderId.OPENCHART)
        assert entry["toProvider"] == ProviderId.OPENCHART.value

    def test_reason_field(self) -> None:
        entry = _call_log_switch(reason=SwitchReason.CIRCUIT_BREAKER_OPEN)
        assert entry["reason"] == SwitchReason.CIRCUIT_BREAKER_OPEN.value

    def test_dataset_field(self) -> None:
        entry = _call_log_switch(dataset=DataType.HISTORICAL_OHLCV)
        assert entry["dataset"] == DataType.HISTORICAL_OHLCV.value

    def test_instrument_id_field(self) -> None:
        entry = _call_log_switch(instrument_id="NSE:RELIANCE:EQ")
        assert entry["instrumentId"] == "NSE:RELIANCE:EQ"

    def test_timestamp_field_present(self) -> None:
        entry = _call_log_switch()
        assert "timestamp" in entry

    def test_timestamp_is_utc_iso8601_with_z_suffix(self) -> None:
        entry = _call_log_switch()
        assert _UTC_ISO8601_MS_RE.match(entry["timestamp"]), (
            f"timestamp '{entry['timestamp']}' does not match UTC ISO-8601 "
            "millisecond format (YYYY-MM-DDTHH:MM:SS.mmmZ)"
        )

    def test_component_is_provider_gateway(self) -> None:
        entry = _call_log_switch()
        assert entry["component"] == "provider_gateway"

    def test_all_required_fields_present(self) -> None:
        """Verify the exact set of fields specified in the design doc."""
        required = {
            "event",
            "fromProvider",
            "toProvider",
            "reason",
            "dataset",
            "instrumentId",
            "timestamp",
        }
        entry = _call_log_switch()
        missing = required - set(entry.keys())
        assert not missing, f"Missing required log fields: {missing}"

    def test_design_doc_example_values(self) -> None:
        """Re-create the example from the design spec and verify the output."""
        entry = _call_log_switch(
            from_provider=ProviderId.ANGEL_ONE,
            to_provider=ProviderId.OPENCHART,
            reason=SwitchReason.CIRCUIT_BREAKER_OPEN,
            dataset=DataType.HISTORICAL_OHLCV,
            instrument_id="NSE:RELIANCE:EQ",
        )
        assert entry["fromProvider"] == "angel_one"
        assert entry["toProvider"] == "openchart"
        assert entry["reason"] == "circuit_breaker_open"
        assert entry["dataset"] == "HISTORICAL_OHLCV"
        assert entry["instrumentId"] == "NSE:RELIANCE:EQ"


# ---------------------------------------------------------------------------
# log_provider_switch — all SwitchReason values accepted
# ---------------------------------------------------------------------------


class TestSwitchReasonEnum:
    @pytest.mark.parametrize("reason", list(SwitchReason))
    def test_all_reasons_accepted(self, reason: SwitchReason) -> None:
        """Every SwitchReason enum value must be accepted without error."""
        entry = _call_log_switch(reason=reason)
        assert entry["reason"] == reason.value

    def test_enum_has_required_members(self) -> None:
        """The required reasons from the spec must all exist."""
        required_names = {
            "CIRCUIT_BREAKER_OPEN",
            "RATE_LIMITED",
            "TIMEOUT",
            "AUTH_FAILED",
            "UNSUPPORTED_CAPABILITY",
            "QUALITY_BELOW_THRESHOLD",
            "MARKET_CLOSED",
        }
        actual_names = {m.name for m in SwitchReason}
        assert required_names <= actual_names, (
            f"Missing SwitchReason members: {required_names - actual_names}"
        )

    def test_circuit_breaker_open_value(self) -> None:
        assert SwitchReason.CIRCUIT_BREAKER_OPEN == "circuit_breaker_open"

    def test_rate_limited_value(self) -> None:
        assert SwitchReason.RATE_LIMITED == "rate_limited"

    def test_timeout_value(self) -> None:
        assert SwitchReason.TIMEOUT == "timeout"

    def test_auth_failed_value(self) -> None:
        assert SwitchReason.AUTH_FAILED == "auth_failed"

    def test_unsupported_capability_value(self) -> None:
        assert SwitchReason.UNSUPPORTED_CAPABILITY == "unsupported_capability"

    def test_quality_below_threshold_value(self) -> None:
        assert SwitchReason.QUALITY_BELOW_THRESHOLD == "quality_below_threshold"

    def test_market_closed_value(self) -> None:
        assert SwitchReason.MARKET_CLOSED == "market_closed"


# ---------------------------------------------------------------------------
# log_provider_switch — timestamp format correctness
# ---------------------------------------------------------------------------


class TestTimestampFormat:
    def test_timestamp_has_milliseconds(self) -> None:
        entry = _call_log_switch()
        ts: str = entry["timestamp"]
        # e.g. "2026-01-15T09:15:00.123Z" — dot + 3 digits + Z
        assert re.search(r"\.\d{3}Z$", ts), (
            f"Timestamp '{ts}' must end with .mmmZ"
        )

    def test_timestamp_has_t_separator(self) -> None:
        entry = _call_log_switch()
        assert "T" in entry["timestamp"], "Timestamp must use T as date/time separator"

    def test_timestamp_ends_with_z(self) -> None:
        entry = _call_log_switch()
        assert entry["timestamp"].endswith("Z"), "Timestamp must end with Z (UTC)"

    def test_timestamp_has_date_part(self) -> None:
        entry = _call_log_switch()
        # Leading YYYY-MM-DD
        assert re.match(r"^\d{4}-\d{2}-\d{2}T", entry["timestamp"]), (
            "Timestamp must start with YYYY-MM-DDT"
        )

    def test_two_switches_timestamps_are_valid_iso8601(self) -> None:
        """Both entries from two separate calls must be valid ISO-8601."""
        entry1 = _call_log_switch()
        entry2 = _call_log_switch()
        for entry in (entry1, entry2):
            assert _UTC_ISO8601_MS_RE.match(entry["timestamp"])


# ---------------------------------------------------------------------------
# get_provider_health stub
# ---------------------------------------------------------------------------


class TestGetProviderHealthStub:
    def test_returns_dict(self) -> None:
        gw = ProviderGateway()
        result = gw.get_provider_health()
        assert isinstance(result, dict)

    def test_covers_all_provider_ids(self) -> None:
        """Every ProviderId must appear as a prefix in at least one key.

        Keys are now compound: '{provider}:{data_type}' (task 4.9).
        """
        gw = ProviderGateway()
        result = gw.get_provider_health()
        # Extract provider prefixes from compound keys.
        provider_prefixes = {key.split(":")[0] for key in result}
        for provider in ProviderId:
            assert provider.value in provider_prefixes, (
                f"ProviderId.{provider.name} ({provider.value}) missing from health dict"
            )

    def test_each_entry_has_status_field(self) -> None:
        gw = ProviderGateway()
        for _key, health in gw.get_provider_health().items():
            assert "status" in health

    def test_each_entry_has_circuit_state_field(self) -> None:
        gw = ProviderGateway()
        for _key, health in gw.get_provider_health().items():
            assert "circuitState" in health


# ---------------------------------------------------------------------------
# fetch stub
# ---------------------------------------------------------------------------


class TestFetchStub:
    @pytest.mark.asyncio
    async def test_fetch_raises_not_implemented(self) -> None:
        gw = ProviderGateway()
        with pytest.raises(NotImplementedError):
            await gw.fetch(
                ProviderId.ANGEL_ONE,
                DataType.HISTORICAL_OHLCV,
                "EQ",
                symbol="RELIANCE",
            )

    @pytest.mark.asyncio
    async def test_fetch_error_message_contains_provider(self) -> None:
        gw = ProviderGateway()
        with pytest.raises(NotImplementedError, match="angel_one"):
            await gw.fetch(
                ProviderId.ANGEL_ONE,
                DataType.LIVE_QUOTE,
                "EQ",
            )
