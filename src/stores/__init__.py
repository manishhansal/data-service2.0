"""
src/stores
==========

Persistent and in-memory data stores used by the platform engines.

Currently exposed:
    LineageStore — in-memory LRU + PostgreSQL lineage / provenance store
                   (Requirements 8.3, 8.6, 10.2).
"""

from src.stores.lineage_store import LineageStore

__all__ = ["LineageStore"]
