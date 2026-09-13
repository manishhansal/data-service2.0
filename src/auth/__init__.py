"""
src/auth — Consumer authentication package for DATA-SERVICE 2.0.

Provides:
    - ApiKeyAuth       — validates X-API-KEY header against the configured allowlist
    - JwtBearerAuth    — validates HS256 JWT in Authorization: Bearer <token>
    - ConsumerAuthDependency — FastAPI dependency that accepts either method

Requirements: 19.2
"""

from src.auth.consumer_auth import (
    ApiKeyAuth,
    ConsumerAuthDependency,
    JwtBearerAuth,
    router,
)

__all__ = [
    "ApiKeyAuth",
    "ConsumerAuthDependency",
    "JwtBearerAuth",
    "router",
]
