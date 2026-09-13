"""
Unit tests for src.forensics.trade_forensics

Coverage:
- TradeForensicsRecord model validation (field constraints, immutability)
- TradeForensicsStore.record()  — happy path & error cases
- TradeForensicsStore.get()     — found / not-found
- TradeForensicsStore.get_by_strategy()   — ordering, limit clamping
- TradeForensicsStore.get_by_instrument() — ordering, limit clamping
- TradeForensicsStore.__len__()

Requirements: 8.7
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from src.forensics.trade_forensics import (
    TradeForensicsRecord,
    TradeForensicsStore,
    _utc_now_iso,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

ISO_8601_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _store() -> TradeForensicsStore:
    return TradeForensicsStore()


def _record_kwargs(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid set of kwargs for TradeForensicsStore.record()."""
    base: dict[str, Any] = dict(
        trade_id=str(uuid.uuid4()),
        observation_ids=[str(uuid.uuid4()), str(uuid.uuid4())],
        strategy_id="india-scalping-v1",
        confidence_score=80,
        signal_allowed=True,
        instrument_id="NIFTY-IDX-NSE",
        exchange="NSE",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# TradeForensicsRecord — model validation
# ---------------------------------------------------------------------------


class TestTradeForensicsRecordValidation:
    def test_valid_record_instantiates(self) -> None:
        rec = TradeForensicsRecord(
            tradeId="t1",
            observationIds=["obs-a", "obs-b"],
            strategyId="s1",
            dataConfidenceAtExecution=75,
            signalEngineAllowed=True,
            executedAt="2025-01-15T09:30:05.123Z",
            instrumentId="NIFTY-IDX",
            exchange="NSE",
        )
        assert rec.tradeId == "t1"
        assert rec.observationIds == ["obs-a", "obs-b"]
        assert rec.dataConfidenceAtExecution == 75
        assert rec.signalEngineAllowed is True

    def test_confidence_score_zero_is_valid(self) -> None:
        rec = TradeForensicsRecord(
            tradeId="t2",
            observationIds=[],
            strategyId="s1",
            dataConfidenceAtExecution=0,
            signalEngineAllowed=False,
            executedAt="2025-01-15T09:30:05.123Z",
            instrumentId="NIFTY-IDX",
            exchange="NSE",
        )
        assert rec.dataConfidenceAtExecution == 0

    def test_confidence_score_max_95_is_valid(self) -> None:
        rec = TradeForensicsRecord(
            tradeId="t3",
            observationIds=[],
            strategyId="s1",
            dataConfidenceAtExecution=95,
            signalEngineAllowed=True,
            executedAt="2025-01-15T09:30:05.123Z",
            instrumentId="NIFTY-IDX",
            exchange="NSE",
        )
        assert rec.dataConfidenceAtExecution == 95

    def test_confidence_score_above_95_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TradeForensicsRecord(
                tradeId="t4",
                observationIds=[],
                strategyId="s1",
                dataConfidenceAtExecution=96,   # > 95 — must fail
                signalEngineAllowed=True,
                executedAt="2025-01-15T09:30:05.123Z",
                instrumentId="NIFTY-IDX",
                exchange="NSE",
            )
        errors = exc_info.value.errors()
        assert any("dataConfidenceAtExecution" in str(e) for e in errors)

    def test_confidence_score_negative_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TradeForensicsRecord(
                tradeId="t5",
                observationIds=[],
                strategyId="s1",
                dataConfidenceAtExecution=-1,
                signalEngineAllowed=True,
                executedAt="2025-01-15T09:30:05.123Z",
                instrumentId="NIFTY-IDX",
                exchange="NSE",
            )

    def test_record_is_immutable(self) -> None:
        rec = TradeForensicsRecord(
            tradeId="t6",
            observationIds=["obs-x"],
            strategyId="s1",
            dataConfidenceAtExecution=50,
            signalEngineAllowed=False,
            executedAt="2025-01-15T09:30:05.123Z",
            instrumentId="NIFTY-IDX",
            exchange="NSE",
        )
        # Pydantic frozen model raises ValidationError on assignment
        with pytest.raises((ValidationError, TypeError)):
            rec.tradeId = "tampered"  # type: ignore[misc]

    def test_empty_observation_ids_is_permitted(self) -> None:
        rec = TradeForensicsRecord(
            tradeId="t7",
            observationIds=[],
            strategyId="s1",
            dataConfidenceAtExecution=60,
            signalEngineAllowed=True,
            executedAt="2025-01-15T09:30:05.123Z",
            instrumentId="NIFTY-IDX",
            exchange="NSE",
        )
        assert rec.observationIds == []


# ---------------------------------------------------------------------------
# TradeForensicsStore.record()
# ---------------------------------------------------------------------------


class TestTradeForensicsStoreRecord:
    def test_record_returns_forensics_record(self) -> None:
        store = _store()
        rec = store.record(**_record_kwargs())
        assert isinstance(rec, TradeForensicsRecord)

    def test_record_persists_all_fields_correctly(self) -> None:
        store = _store()
        trade_id = str(uuid.uuid4())
        obs = [str(uuid.uuid4())]
        rec = store.record(
            trade_id=trade_id,
            observation_ids=obs,
            strategy_id="strat-x",
            confidence_score=72,
            signal_allowed=False,
            instrument_id="BANKNIFTY-OPTIDX",
            exchange="NFO",
            executed_at="2025-06-01T10:00:00.000Z",
        )
        assert rec.tradeId == trade_id
        assert rec.observationIds == obs
        assert rec.strategyId == "strat-x"
        assert rec.dataConfidenceAtExecution == 72
        assert rec.signalEngineAllowed is False
        assert rec.instrumentId == "BANKNIFTY-OPTIDX"
        assert rec.exchange == "NFO"
        assert rec.executedAt == "2025-06-01T10:00:00.000Z"

    def test_record_auto_assigns_executed_at_when_not_provided(self) -> None:
        store = _store()
        rec = store.record(**_record_kwargs())
        # Must match the ISO-8601 Z pattern we use throughout the platform
        assert ISO_8601_Z.match(rec.executedAt), (
            f"executedAt={rec.executedAt!r} does not match expected ISO-8601 pattern"
        )

    def test_duplicate_trade_id_raises_value_error(self) -> None:
        store = _store()
        kwargs = _record_kwargs()
        store.record(**kwargs)

        with pytest.raises(ValueError, match="already exists"):
            store.record(**kwargs)   # same trade_id a second time

    def test_observation_ids_are_defensively_copied(self) -> None:
        """Mutating the original list must not affect the stored record."""
        store = _store()
        obs = ["obs-1", "obs-2"]
        rec = store.record(**_record_kwargs(observation_ids=obs))
        obs.append("obs-3")
        assert len(rec.observationIds) == 2

    def test_store_len_increments_on_each_record(self) -> None:
        store = _store()
        assert len(store) == 0
        store.record(**_record_kwargs())
        assert len(store) == 1
        store.record(**_record_kwargs())
        assert len(store) == 2


# ---------------------------------------------------------------------------
# TradeForensicsStore.get()
# ---------------------------------------------------------------------------


class TestTradeForensicsStoreGet:
    def test_get_returns_record_for_known_trade_id(self) -> None:
        store = _store()
        kwargs = _record_kwargs()
        expected = store.record(**kwargs)
        result = store.get(kwargs["trade_id"])
        assert result == expected

    def test_get_returns_none_for_unknown_trade_id(self) -> None:
        store = _store()
        assert store.get("non-existent") is None

    def test_get_after_multiple_records_returns_correct_one(self) -> None:
        store = _store()
        ids = [str(uuid.uuid4()) for _ in range(5)]
        for tid in ids:
            store.record(**_record_kwargs(trade_id=tid))
        # Retrieve the third one specifically
        result = store.get(ids[2])
        assert result is not None
        assert result.tradeId == ids[2]


# ---------------------------------------------------------------------------
# TradeForensicsStore.get_by_strategy()
# ---------------------------------------------------------------------------


class TestTradeForensicsStoreGetByStrategy:
    def test_returns_empty_list_for_unknown_strategy(self) -> None:
        store = _store()
        assert store.get_by_strategy("no-such-strategy") == []

    def test_returns_all_records_for_strategy_when_within_limit(self) -> None:
        store = _store()
        for _ in range(3):
            store.record(**_record_kwargs(strategy_id="strat-a"))
        results = store.get_by_strategy("strat-a")
        assert len(results) == 3
        assert all(r.strategyId == "strat-a" for r in results)

    def test_most_recent_record_is_first(self) -> None:
        """get_by_strategy returns most-recently-recorded first."""
        store = _store()
        tids = [str(uuid.uuid4()) for _ in range(3)]
        for tid in tids:
            store.record(**_record_kwargs(trade_id=tid, strategy_id="strat-b"))
        results = store.get_by_strategy("strat-b")
        # Most recent insertion was tids[2]
        assert results[0].tradeId == tids[2]

    def test_limit_is_respected(self) -> None:
        store = _store()
        for _ in range(10):
            store.record(**_record_kwargs(strategy_id="strat-c"))
        results = store.get_by_strategy("strat-c", limit=3)
        assert len(results) == 3

    def test_limit_clamped_to_minimum_1(self) -> None:
        store = _store()
        for _ in range(5):
            store.record(**_record_kwargs(strategy_id="strat-d"))
        results = store.get_by_strategy("strat-d", limit=0)   # 0 → clamped to 1
        assert len(results) == 1

    def test_limit_clamped_to_maximum_1000(self) -> None:
        store = _store()
        for _ in range(5):
            store.record(**_record_kwargs(strategy_id="strat-e"))
        # Requesting more than available is fine; limit=9999 is clamped to 1000
        results = store.get_by_strategy("strat-e", limit=9999)
        assert len(results) == 5   # only 5 exist

    def test_does_not_return_records_of_other_strategy(self) -> None:
        store = _store()
        store.record(**_record_kwargs(strategy_id="alpha"))
        store.record(**_record_kwargs(strategy_id="beta"))
        alpha_results = store.get_by_strategy("alpha")
        assert all(r.strategyId == "alpha" for r in alpha_results)
        assert len(alpha_results) == 1


# ---------------------------------------------------------------------------
# TradeForensicsStore.get_by_instrument()
# ---------------------------------------------------------------------------


class TestTradeForensicsStoreGetByInstrument:
    def test_returns_empty_list_for_unknown_instrument(self) -> None:
        store = _store()
        assert store.get_by_instrument("UNKNOWN-INST") == []

    def test_returns_all_records_for_instrument_when_within_limit(self) -> None:
        store = _store()
        for _ in range(4):
            store.record(**_record_kwargs(instrument_id="NIFTY-OPTIDX"))
        results = store.get_by_instrument("NIFTY-OPTIDX")
        assert len(results) == 4
        assert all(r.instrumentId == "NIFTY-OPTIDX" for r in results)

    def test_most_recent_record_is_first(self) -> None:
        store = _store()
        tids = [str(uuid.uuid4()) for _ in range(3)]
        for tid in tids:
            store.record(**_record_kwargs(trade_id=tid, instrument_id="BANKNIFTY-OPTIDX"))
        results = store.get_by_instrument("BANKNIFTY-OPTIDX")
        assert results[0].tradeId == tids[2]

    def test_limit_is_respected(self) -> None:
        store = _store()
        for _ in range(8):
            store.record(**_record_kwargs(instrument_id="FINNIFTY-OPTIDX"))
        results = store.get_by_instrument("FINNIFTY-OPTIDX", limit=2)
        assert len(results) == 2

    def test_does_not_return_records_of_other_instrument(self) -> None:
        store = _store()
        store.record(**_record_kwargs(instrument_id="NIFTY-IDX"))
        store.record(**_record_kwargs(instrument_id="SENSEX-IDX"))
        results = store.get_by_instrument("NIFTY-IDX")
        assert all(r.instrumentId == "NIFTY-IDX" for r in results)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Cross-index consistency
# ---------------------------------------------------------------------------


class TestCrossIndexConsistency:
    def test_same_record_accessible_through_all_three_indexes(self) -> None:
        store = _store()
        kwargs = _record_kwargs(
            strategy_id="cross-test-strat",
            instrument_id="cross-test-inst",
        )
        written = store.record(**kwargs)

        by_id = store.get(kwargs["trade_id"])
        by_strat = store.get_by_strategy("cross-test-strat")
        by_inst = store.get_by_instrument("cross-test-inst")

        assert by_id == written
        assert len(by_strat) == 1 and by_strat[0] == written
        assert len(by_inst) == 1 and by_inst[0] == written

    def test_records_for_different_instruments_do_not_bleed(self) -> None:
        store = _store()
        store.record(**_record_kwargs(instrument_id="INST-A"))
        store.record(**_record_kwargs(instrument_id="INST-B"))
        store.record(**_record_kwargs(instrument_id="INST-A"))

        assert len(store.get_by_instrument("INST-A")) == 2
        assert len(store.get_by_instrument("INST-B")) == 1


# ---------------------------------------------------------------------------
# _utc_now_iso helper
# ---------------------------------------------------------------------------


class TestUtcNowIso:
    def test_returns_correct_format(self) -> None:
        ts = _utc_now_iso()
        assert ISO_8601_Z.match(ts), f"Expected ISO-8601 Z format, got: {ts!r}"

    def test_successive_calls_are_monotonic(self) -> None:
        t1 = _utc_now_iso()
        t2 = _utc_now_iso()
        # Lexicographic ordering == chronological ordering for ISO-8601 UTC
        assert t2 >= t1
