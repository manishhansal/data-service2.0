"""
src/core/schemas/parity.py

DataParityContract — the authoritative document that declares every
correctness invariant consumers (primarily AlphaForge) can rely on when
integrating with DATA-SERVICE 2.0.

The contract is intentionally static: it is generated once at process start
with a fixed ``generatedAt`` timestamp and served verbatim via
``GET /v1/contract``.  This makes it machine-readable and diffable across
deployments.

Key invariants captured in the contract (non-negotiable):
    * The ``3m`` interval is permanently unsupported for Indian market data
      at every layer — acquisition, normaliser, persistence, and API.
    * ``oi`` is never populated from ``tradedValue``; they are semantically
      distinct fields (open interest vs. total traded value in INR).
    * ``iv`` (implied volatility) is ``null`` when not supplied by the
      provider; zero is never used as a substitute for a missing IV.
    * All other optional numeric fields (``delta``, ``gamma``, ``theta``,
      ``vega``, ``rho``, ``bid``, ``ask``) are ``null`` when absent —
      placeholder zeros are prohibited.
    * Every response includes ``dataSourceType``, ``provenance``, ``quality``,
      and ``freshness`` metadata.

Requirements: 1.5, 6.2, 6.4, 23.1, 23.2
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Platform constants
# ---------------------------------------------------------------------------

#: Current semver version of the DataParityContract itself.
CONTRACT_VERSION: str = "2.0.0"

#: The ``/v1/`` API version consumers must target.
MARKET_DATA_API_VERSION: str = "v1"

#: Canonical intervals supported for Indian market data.
#: ``3m`` is **permanently excluded** from this list (Requirement 1.5, 4.2).
INDIA_INTERVALS: list[str] = ["1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M"]

#: Intervals supported for Binance crypto data.
#: ``3m`` is explicitly allowed here because Binance natively supports it
#: and the ``3m`` restriction applies **only** to Indian market data.
CRYPTO_INTERVALS: list[str] = [
    "1m", "3m", "5m", "15m", "30m", "1h",
    "2h", "4h", "6h", "8h", "12h", "1d",
]

#: Human-readable guarantees that every consumer may rely on.
GUARANTEES: list[str] = [
    "Every response envelope includes dataSourceType, provenance, quality, and freshness metadata.",
    "No field is ever fabricated; absent fields are null, not zero or a substitute value.",
    "The oi (open interest) field is never populated from tradedValue; they are semantically distinct.",
    "The iv (implied volatility) field is null when the provider does not supply it; zero IV is prohibited as a substitute.",
    "Option Greeks (delta, gamma, theta, vega, rho) are null when not provided; placeholder zeros are prohibited.",
    "bid and ask prices are null when not actually provided by the source; placeholder zeros are prohibited.",
    "All timestamps in API responses are UTC ISO-8601 strings with Z suffix.",
    "All datasets traverse the 14-step validation pipeline before delivery or persistence.",
    "Duplicate observations are published with isDuplicate:true rather than silently dropped.",
    "DataConfidenceScore is capped at 95 — a score of 100 is never emitted to reflect inherent market-data uncertainty.",
    "A DataConfidenceScore below 30 (grade BLOCKED) always sets signalEngineAllowed:false with no exceptions.",
    "Live, paper-trading, replay, and backtest modes all route through the same normalisation and validation pipeline.",
    "Backtest data for time T contains only records where availableAtMs <= T (no look-ahead bias).",
]

#: Intervals explicitly prohibited per market.
PROHIBITED_INTERVALS: dict[str, list[str]] = {
    "india": ["3m"],
    # No prohibitions for crypto — Binance natively supports 3m.
    "crypto": [],
}

#: Canonical statement on null semantics.
NULL_SEMANTICS: str = (
    "All optional numeric and object fields are null when the upstream provider "
    "does not supply a value. Zero is never used as a substitute for a missing "
    "value unless zero is genuinely the correct measured value (e.g. volume == 0 "
    "on a genuine zero-volume bar). When a volume value is unavailable "
    "(as opposed to genuinely zero), volumeUnavailable:true is set and volume "
    "is stored as 0 to distinguish it from a genuine zero-volume bar."
)

#: OI semantic integrity rule.
OI_SOURCE_RULE: str = (
    "The oi (open interest in contracts) field is NEVER populated from the "
    "tradedValue field. tradedValue represents total traded value in INR. "
    "These two fields are semantically distinct and must never be used "
    "interchangeably. When the provider does not supply oi, the field is set "
    "to null and oiMissing:true is set on the containing object."
)

#: IV semantic integrity rule.
IV_RULE: str = (
    "The iv (implied volatility) field is null when the provider does not "
    "explicitly supply it. Zero IV is never used as a substitute for a missing "
    "IV value. If the provider explicitly returns 0.0 as the IV, that value is "
    "preserved; any inferred or fabricated zero is rejected by the semantic "
    "validation step of the pipeline."
)


# ---------------------------------------------------------------------------
# DataParityContract model
# ---------------------------------------------------------------------------


class DataParityContract(BaseModel):
    """Machine-readable data-correctness contract for DATA-SERVICE 2.0.

    Declares all invariants, semantic rules, and supported intervals that
    consumers (primarily AlphaForge) may rely on when integrating with the
    Platform.  Served read-only via ``GET /v1/contract``.

    Attributes:
        contractVersion:       Semver version of the contract document itself.
        marketDataApiVersion:  The ``/v1/`` prefix consumers must target.
        supportedMarkets:      Top-level market verticals: ``["india", "crypto"]``.
        supportedIntervals:    Canonical intervals per market.  India list
                               never contains ``3m``; crypto list includes it.
        guarantees:            Human-readable invariant statements consumers
                               may treat as contractual commitments.
        prohibitedIntervals:   Explicitly banned intervals per market.
                               ``{"india": ["3m"]}`` — ``3m`` is permanently
                               unsupported for Indian market data at every layer.
        nullSemantics:         Canonical statement on null-vs-zero semantics.
        oiSourceRule:          OI is never populated from tradedValue.
        ivRule:                IV is null when absent; zero is not a substitute.
        generatedAt:           UTC ISO-8601 timestamp when this contract
                               instance was produced.
        parityVerified:        True when all modes (live, paper, replay,
                               backtest) have been verified to share the same
                               normalisation and validation pipeline.
        lastVerifiedAt:        UTC ISO-8601 timestamp of the last successful
                               parity verification; null when never verified.
        liveDataPath:          Module path for the live data pipeline entry point.
        paperDataPath:         Module path for the paper-trading data pipeline.
        replayDataPath:        Module path for the replay engine pipeline.
        backtestDataPath:      Module path for the backtest engine pipeline.
    """

    model_config = ConfigDict(
        frozen=True,          # contract is read-only once produced
        populate_by_name=True,
    )

    # ── Contract identity ────────────────────────────────────────────────────
    contractVersion: str = Field(
        default=CONTRACT_VERSION,
        description="Semver version of the DataParityContract document.",
    )
    marketDataApiVersion: str = Field(
        default=MARKET_DATA_API_VERSION,
        description="API version prefix consumers must use (e.g. 'v1').",
    )

    # ── Market and interval coverage ─────────────────────────────────────────
    supportedMarkets: list[str] = Field(
        default_factory=lambda: ["india", "crypto"],
        description="Top-level market verticals supported by the Platform.",
    )
    supportedIntervals: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "india": INDIA_INTERVALS,
            "crypto": CRYPTO_INTERVALS,
        },
        description=(
            "Canonical intervals per market. "
            "India list never contains '3m' (permanently unsupported). "
            "Crypto list includes '3m' (Binance native support)."
        ),
    )

    # ── Correctness guarantees ───────────────────────────────────────────────
    guarantees: list[str] = Field(
        default_factory=lambda: list(GUARANTEES),
        description="Human-readable invariant statements consumers may rely on.",
    )
    prohibitedIntervals: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "india": ["3m"],
            "crypto": [],
        },
        description=(
            "Explicitly banned intervals per market. "
            "'3m' is permanently banned for India at every processing layer."
        ),
    )

    # ── Semantic integrity rules ─────────────────────────────────────────────
    nullSemantics: str = Field(
        default=NULL_SEMANTICS,
        description="Canonical statement on null vs. zero field semantics.",
    )
    oiSourceRule: str = Field(
        default=OI_SOURCE_RULE,
        description="OI is never populated from tradedValue; semantically distinct.",
    )
    ivRule: str = Field(
        default=IV_RULE,
        description="IV is null when absent; zero is not a valid substitute.",
    )

    # ── Parity verification ──────────────────────────────────────────────────
    parityVerified: bool = Field(
        default=False,
        description=(
            "True when live, paper, replay, and backtest modes have been verified "
            "to share the same normalisation and validation pipeline. "
            "False when never verified or when a divergence is detected."
        ),
    )
    lastVerifiedAt: Optional[str] = Field(
        default=None,
        description=(
            "UTC ISO-8601 timestamp of the last successful parity verification. "
            "Null when the contract has never been verified."
        ),
    )

    # ── Data path declarations ───────────────────────────────────────────────
    liveDataPath: str = Field(
        default="src.engines.market_engine",
        description="Module path for the live data pipeline entry point.",
    )
    paperDataPath: str = Field(
        default="src.engines.market_engine",
        description=(
            "Module path for the paper-trading data pipeline. "
            "Identical to liveDataPath — parity enforced by design."
        ),
    )
    replayDataPath: str = Field(
        default="src.engines.replay_engine",
        description="Module path for the replay engine pipeline.",
    )
    backtestDataPath: str = Field(
        default="src.engines.backtest_engine",
        description="Module path for the backtest engine pipeline.",
    )

    # ── Generation timestamp ─────────────────────────────────────────────────
    generatedAt: str = Field(
        description="UTC ISO-8601 timestamp when this contract instance was produced.",
    )


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------


def build_contract(
    *,
    parity_verified: bool = False,
    last_verified_at: Optional[str] = None,
) -> DataParityContract:
    """Build and return the current :class:`DataParityContract` instance.

    Parameters
    ----------
    parity_verified:
        Set to ``True`` when all four data modes (live, paper, replay,
        backtest) have been verified to share the same pipeline.
    last_verified_at:
        UTC ISO-8601 string of the last successful parity verification.
        Pass ``None`` when the contract has never been verified.

    Returns
    -------
    DataParityContract
        Fully-populated, immutable contract instance with ``generatedAt``
        set to the current UTC instant.
    """
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    return DataParityContract(
        parityVerified=parity_verified,
        lastVerifiedAt=last_verified_at,
        generatedAt=generated_at,
    )
