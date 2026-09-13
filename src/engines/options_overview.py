"""
src/engines/options_overview.py

DeribitOptionsOverview Engine — Task 12.2

Computes a summarised options overview for a given crypto currency (BTC, ETH,
SOL) using live data fetched from the Deribit REST API via
:class:`~src.providers.deribit_client.DeribitClient`.

Key design invariants (Requirements 14.1, 14.4, 14.5, 6.2, 6.4, 6.6):
  - ``mark_iv`` is ``None`` when Deribit does not supply it; zero is never
    substituted for a missing IV value.
  - ``open_interest`` is ``None`` when absent; zero is never substituted.
  - ``best_bid`` / ``best_ask`` are ``None`` when not provided by Deribit;
    placeholder zeros are prohibited.
  - ``bid_ask_spread`` is only computed when **both** ``best_bid`` and
    ``best_ask`` are present and both are > 0.
  - ``put_call_oi_ratio`` is ``None`` (not zero, not infinity) when
    ``total_call_oi`` is zero or when there are no call contracts with
    non-null OI, signalling that the ratio is genuinely unavailable.

Public API
----------
:class:`OptionContractSummary`
    Pydantic v2 model representing a single normalised option contract.

:class:`OptionsOverviewResult`
    Pydantic v2 model aggregating all contracts with computed analytics.

:class:`DeribitOptionsOverview`
    Async computation class.  Call ``await compute(currency, deribit_client)``
    to obtain an :class:`OptionsOverviewResult`.

Requirements: 14.1, 14.2, 14.4, 14.5, 6.2, 6.4, 6.6
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.observability.logging import get_logger
from src.providers.deribit_client import DeribitClient

logger = get_logger(__name__)

__all__ = [
    "OptionContractSummary",
    "OptionsOverviewResult",
    "DeribitOptionsOverview",
]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class OptionContractSummary(BaseModel):
    """Normalised summary for a single Deribit option contract.

    All nullable fields preserve ``None`` exactly as received from Deribit —
    no zero-substitution is ever applied (Requirements 14.5, 6.4, 6.6).

    Fields
    ------
    instrument_name:
        The raw Deribit instrument name, e.g. ``"BTC-27DEC24-100000-C"``.
    expiry:
        Expiry date as an ISO-8601 date string, e.g. ``"2024-12-27"``.
        Derived from the Deribit ``expiration_timestamp`` (UTC epoch ms).
    strike:
        Strike price as a float.
    option_type:
        ``"call"`` or ``"put"``.
    mark_price:
        Mark price from Deribit ticker, or ``None`` when absent.
    mark_iv:
        Implied volatility in percent (e.g. ``85.5`` means 85.5%), or
        ``None`` when Deribit does not supply it.  **Never zero-substituted.**
    open_interest:
        Open interest in contracts, or ``None`` when absent.
        **Never zero-substituted.**
    best_bid:
        Best bid price, or ``None`` when not provided.
        **Never zero-substituted.**
    best_ask:
        Best ask price, or ``None`` when not provided.
        **Never zero-substituted.**
    bid_ask_spread:
        ``ask - bid`` when both ``best_bid`` and ``best_ask`` are present
        and > 0; ``None`` otherwise.  Computed by the model validator.
    """

    model_config = ConfigDict(frozen=True)

    instrument_name: str
    expiry: str  # ISO-8601 date, e.g. "2024-12-27"
    strike: float
    option_type: str  # "call" or "put"
    mark_price: float | None = None
    mark_iv: float | None = None
    open_interest: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    bid_ask_spread: float | None = None

    @field_validator("option_type")
    @classmethod
    def _validate_option_type(cls, v: str) -> str:
        if v not in ("call", "put"):
            raise ValueError(f"option_type must be 'call' or 'put', got {v!r}")
        return v

    @model_validator(mode="before")
    @classmethod
    def _compute_spread(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Compute bid_ask_spread from bid/ask when both are present and > 0."""
        bid = data.get("best_bid")
        ask = data.get("best_ask")
        # Only compute the spread if the caller hasn't already supplied it
        if "bid_ask_spread" not in data or data["bid_ask_spread"] is None:
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                data["bid_ask_spread"] = ask - bid
            else:
                data["bid_ask_spread"] = None
        return data


class OptionsOverviewResult(BaseModel):
    """Aggregated options overview for a single crypto currency.

    Fields
    ------
    currency:
        Three-letter currency code: ``"BTC"``, ``"ETH"``, or ``"SOL"``.
    index_price:
        Current spot/index price from Deribit (e.g. ``"btc_usd"`` index).
    contracts:
        List of normalised :class:`OptionContractSummary` objects — one per
        Deribit option instrument.
    total_call_oi:
        Sum of ``open_interest`` across all call contracts; contracts with
        ``None`` OI contribute 0 to this sum (they are excluded from the
        numerator of the PCR but do not cause an error).
    total_put_oi:
        Sum of ``open_interest`` across all put contracts (same convention).
    put_call_oi_ratio:
        ``total_put_oi / total_call_oi``, or ``None`` when
        ``total_call_oi == 0``.  ``None`` signals genuine unavailability
        rather than a zero or infinity value (Requirement 14.4).
    computed_at:
        UTC ISO-8601 timestamp (with ``Z`` suffix) when this result was
        computed, e.g. ``"2024-12-27T10:30:00.000000Z"``.
    """

    model_config = ConfigDict(frozen=True)

    currency: str
    index_price: float
    contracts: list[OptionContractSummary]
    total_call_oi: float
    total_put_oi: float
    put_call_oi_ratio: float | None
    computed_at: str  # UTC ISO-8601 with Z suffix

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, v: str) -> str:
        allowed = {"BTC", "ETH", "SOL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(
                f"currency must be one of {sorted(allowed)}, got {v!r}"
            )
        return upper


# ---------------------------------------------------------------------------
# DeribitOptionsOverview
# ---------------------------------------------------------------------------


class DeribitOptionsOverview:
    """Computes an :class:`OptionsOverviewResult` by querying Deribit.

    This class is stateless — it holds no cached state between calls.  Each
    invocation of :meth:`compute` fetches fresh data from Deribit.

    Usage
    -----
    ::

        engine = DeribitOptionsOverview()
        async with DeribitClient() as client:
            result = await engine.compute("BTC", client)

    Concurrency
    -----------
    Ticker requests for each instrument are issued *sequentially* to avoid
    overwhelming Deribit's public rate limit (5 req/s).  For production use
    at scale, the caller should consider using the bulk ``get_book_summary``
    endpoint and wiring it through here instead.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compute(
        self,
        currency: str,
        deribit_client: DeribitClient,
    ) -> OptionsOverviewResult:
        """Fetch Deribit data and produce an :class:`OptionsOverviewResult`.

        Steps
        -----
        1. Fetch all option instruments for *currency* via
           ``get_instruments(currency, kind="option")``.
        2. Fetch the current index price via
           ``get_index_price("{currency.lower()}_usd")``.
        3. For each instrument, fetch ticker data via
           ``get_ticker(instrument_name)`` to extract ``mark_price``,
           ``mark_iv``, ``open_interest``, ``best_bid_price``,
           ``best_ask_price``.
        4. Assemble :class:`OptionContractSummary` objects, preserving
           ``None`` for any absent field.
        5. Compute aggregate OI totals and ``put_call_oi_ratio``.
        6. Return :class:`OptionsOverviewResult`.

        Parameters
        ----------
        currency:
            Three-letter currency code, e.g. ``"BTC"``.  Case-insensitive;
            will be uppercased internally.
        deribit_client:
            An open :class:`~src.providers.deribit_client.DeribitClient`
            instance.  The caller owns its lifecycle.

        Returns
        -------
        OptionsOverviewResult
            A fully populated result object.

        Raises
        ------
        ValueError
            If Deribit returns an application-level error on the instruments
            or index-price calls.
        httpx.HTTPStatusError
            For non-recoverable HTTP errors (4xx) on any underlying call.
        """
        currency_upper = currency.upper()

        logger.info(
            "deribit_options_overview.compute.start",
            component="deribit_options_overview",
            currency=currency_upper,
        )

        # Step 1: fetch instruments
        instruments = await deribit_client.get_instruments(
            currency=currency_upper,
            kind="option",
        )

        logger.debug(
            "deribit_options_overview.instruments_fetched",
            component="deribit_options_overview",
            currency=currency_upper,
            count=len(instruments),
        )

        # Step 2: fetch index price
        index_name = f"{currency_upper.lower()}_usd"
        index_data = await deribit_client.get_index_price(index_name)
        index_price: float = float(index_data["index_price"])

        logger.debug(
            "deribit_options_overview.index_price_fetched",
            component="deribit_options_overview",
            currency=currency_upper,
            index_price=index_price,
        )

        # Step 3 & 4: fetch ticker per instrument and build contract summaries
        contracts: list[OptionContractSummary] = []
        for inst in instruments:
            instrument_name: str = inst.get("instrument_name", "")
            if not instrument_name:
                logger.warning(
                    "deribit_options_overview.skip_unnamed_instrument",
                    component="deribit_options_overview",
                    currency=currency_upper,
                    instrument=inst,
                )
                continue

            summary = await self._fetch_contract_summary(
                instrument=inst,
                deribit_client=deribit_client,
            )
            if summary is not None:
                contracts.append(summary)

        # Step 5: aggregate OI and compute ratio
        total_call_oi, total_put_oi = _aggregate_oi(contracts)
        put_call_oi_ratio = _compute_pcr(total_call_oi, total_put_oi)

        # Step 6: assemble result
        computed_at = _utc_iso8601_now()
        result = OptionsOverviewResult(
            currency=currency_upper,
            index_price=index_price,
            contracts=contracts,
            total_call_oi=total_call_oi,
            total_put_oi=total_put_oi,
            put_call_oi_ratio=put_call_oi_ratio,
            computed_at=computed_at,
        )

        logger.info(
            "deribit_options_overview.compute.complete",
            component="deribit_options_overview",
            currency=currency_upper,
            contract_count=len(contracts),
            total_call_oi=total_call_oi,
            total_put_oi=total_put_oi,
            put_call_oi_ratio=put_call_oi_ratio,
        )

        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_contract_summary(
        self,
        instrument: dict[str, Any],
        deribit_client: DeribitClient,
    ) -> OptionContractSummary | None:
        """Fetch ticker for a single instrument and build an
        :class:`OptionContractSummary`.

        Returns ``None`` if the ticker fetch fails or the instrument cannot
        be parsed (e.g. missing ``instrument_name``).  A warning is logged in
        that case — failures are silently skipped, not propagated, so that a
        single bad instrument does not abort the entire overview.
        """
        instrument_name: str = instrument.get("instrument_name", "")
        option_type_raw: str = instrument.get("option_type", "")  # "call" or "put"
        strike_raw = instrument.get("strike")
        expiration_ts = instrument.get("expiration_timestamp")  # UTC epoch ms

        # Resolve option type
        option_type = _resolve_option_type(option_type_raw, instrument_name)
        if option_type is None:
            logger.warning(
                "deribit_options_overview.skip_unknown_option_type",
                component="deribit_options_overview",
                instrument_name=instrument_name,
                option_type_raw=option_type_raw,
            )
            return None

        # Resolve strike
        try:
            strike = float(strike_raw) if strike_raw is not None else 0.0
        except (TypeError, ValueError):
            logger.warning(
                "deribit_options_overview.skip_invalid_strike",
                component="deribit_options_overview",
                instrument_name=instrument_name,
                strike_raw=strike_raw,
            )
            return None

        # Resolve expiry date string (ISO-8601)
        expiry = _expiry_to_date_str(expiration_ts, instrument_name)

        # Fetch ticker
        try:
            ticker = await deribit_client.get_ticker(instrument_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "deribit_options_overview.ticker_fetch_failed",
                component="deribit_options_overview",
                instrument_name=instrument_name,
                error=str(exc),
            )
            return None

        # Extract nullable fields — preserve None, never substitute zero
        mark_price: float | None = _extract_float(ticker, "mark_price")
        mark_iv: float | None = _extract_float(ticker, "mark_iv")
        open_interest: float | None = _extract_float(ticker, "open_interest")
        best_bid: float | None = _extract_float(ticker, "best_bid_price")
        best_ask: float | None = _extract_float(ticker, "best_ask_price")

        return OptionContractSummary(
            instrument_name=instrument_name,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            mark_price=mark_price,
            mark_iv=mark_iv,
            open_interest=open_interest,
            best_bid=best_bid,
            best_ask=best_ask,
            # bid_ask_spread is auto-computed by the model_validator
        )


# ---------------------------------------------------------------------------
# Pure helper functions (module-private)
# ---------------------------------------------------------------------------


def _extract_float(data: dict[str, Any], key: str) -> float | None:
    """Return ``float(data[key])`` when the value is a non-None number,
    otherwise ``None``.

    This is the canonical null-preservation helper: it never returns 0 for
    a missing or None Deribit field (Requirements 14.5, 6.4, 6.6).
    """
    value = data.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_option_type(option_type_raw: str, instrument_name: str) -> str | None:
    """Map Deribit ``option_type`` field to ``"call"`` or ``"put"``.

    Deribit returns ``"call"`` or ``"put"`` in the instruments list.  If the
    field is missing or unrecognised, attempt to infer from the instrument
    name suffix (``-C`` → call, ``-P`` → put).  Returns ``None`` when
    unable to resolve.
    """
    normalised = option_type_raw.lower().strip()
    if normalised == "call":
        return "call"
    if normalised == "put":
        return "put"

    # Fallback: infer from instrument name suffix
    if instrument_name.endswith("-C"):
        return "call"
    if instrument_name.endswith("-P"):
        return "put"

    return None


def _expiry_to_date_str(expiration_ts: Any, instrument_name: str) -> str:
    """Convert a Deribit ``expiration_timestamp`` (UTC epoch ms) to an
    ISO-8601 date string (``"YYYY-MM-DD"``).

    Falls back to ``"unknown"`` when the timestamp is absent or unparseable,
    logging a warning.
    """
    if expiration_ts is None:
        logger.warning(
            "deribit_options_overview.missing_expiry_ts",
            component="deribit_options_overview",
            instrument_name=instrument_name,
        )
        return "unknown"

    try:
        ts_ms = int(expiration_ts)
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError) as exc:
        logger.warning(
            "deribit_options_overview.invalid_expiry_ts",
            component="deribit_options_overview",
            instrument_name=instrument_name,
            expiration_timestamp=expiration_ts,
            error=str(exc),
        )
        return "unknown"


def _aggregate_oi(
    contracts: list[OptionContractSummary],
) -> tuple[float, float]:
    """Aggregate open interest across all call and put contracts.

    Contracts with ``open_interest=None`` contribute 0 to their respective
    total (they are absent, not zero-OI).  This distinction matters for
    ``put_call_oi_ratio``: a result of 0 calls with *all* call OI absent is
    semantically different from 0 calls with all call OI explicitly reported
    as zero by the exchange.  In both cases the ratio is ``None``.

    Returns
    -------
    (total_call_oi, total_put_oi) as floats.
    """
    total_call_oi: float = 0.0
    total_put_oi: float = 0.0
    for contract in contracts:
        oi = contract.open_interest if contract.open_interest is not None else 0.0
        if contract.option_type == "call":
            total_call_oi += oi
        else:
            total_put_oi += oi
    return total_call_oi, total_put_oi


def _compute_pcr(total_call_oi: float, total_put_oi: float) -> float | None:
    """Compute put/call OI ratio.

    Returns ``None`` — not zero, not infinity — when ``total_call_oi == 0``
    to signal that the ratio is genuinely unavailable (Requirement 14.4).
    """
    if total_call_oi == 0.0:
        return None
    return total_put_oi / total_call_oi


def _utc_iso8601_now() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
