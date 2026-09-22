"""
src/core/normalizers/

Provider-specific normalizers that convert raw provider responses into
canonical platform schema objects ready for persistence and API serving.

Each normalizer module handles one provider's distinct response format.
All normalizers enforce the same null semantic rules as src/core/normaliser.py:
  - OI: NULL when absent, never from tradedValue
  - IV: NULL when absent, zero IS NOT a substitute
  - Greeks: NULL when absent, placeholder zeros prohibited
  - Bid/ask: NULL when absent, zero prohibited
"""
from src.core.normalizers.angel_one import AngelOneNormalizer
from src.core.normalizers.upstox import UpstoxNormalizer
from src.core.normalizers.freshness import FreshnessClassifier, FreshnessState

__all__ = [
    "AngelOneNormalizer",
    "UpstoxNormalizer",
    "FreshnessClassifier",
    "FreshnessState",
]
