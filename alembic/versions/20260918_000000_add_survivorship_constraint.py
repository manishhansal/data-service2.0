"""Add fc_candle_not_after_expiry CHECK constraint to futures_candle.

Prevents candles from being inserted with a timestamp past the contract
expiry date.  This is the DB-level survivorship bias guard.

  futures_candle.time::date <= futures_candle.expiry

Revision ID: 20260918_000000
Revises: 20260917_100000
Create Date: 2026-09-18
"""
from __future__ import annotations

from alembic import op

revision: str = "20260918_000000"
down_revision: str = "20260917_100000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "fc_candle_not_after_expiry",
        "futures_candle",
        "CAST(time AS date) <= expiry",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fc_candle_not_after_expiry",
        "futures_candle",
        type_="check",
    )
