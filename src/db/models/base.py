"""
src/db/models/base.py

Declarative base for all SQLAlchemy ORM models.
"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base — all ORM models inherit from this."""
    pass
