"""
Middleware package for DATA-SERVICE 2.0.

Provides reusable ASGI middleware components:
- RateLimitMiddleware — sliding-window consumer rate limiting (Task 13.3)
"""

from src.middleware.rate_limiter import RateLimitMiddleware, SlidingWindowRateLimiter

__all__ = ["RateLimitMiddleware", "SlidingWindowRateLimiter"]
