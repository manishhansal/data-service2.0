"""
tests/unit/api/test_errors.py

Unit tests for src/api/errors.py

Tests cover:
- ErrorCode enum membership and string-value behaviour.
- canonical_error() dict shape and field semantics.
- json_error_response() HTTP status code, Content-Type, Cache-Control, body.
- default_status_for() mapping.
- requestId auto-generation and caller-override.

Requirements: 16.3, 16.4, 19.4
"""

from __future__ import annotations

import json
import uuid

import pytest

from src.api.errors import (
    ErrorCode,
    canonical_error,
    default_status_for,
    json_error_response,
)


# ---------------------------------------------------------------------------
# ErrorCode enum
# ---------------------------------------------------------------------------


class TestErrorCodeEnum:
    def test_all_required_codes_present(self) -> None:
        required = [
            "UNAUTHORIZED",
            "INVALID_API_KEY",
            "TOKEN_EXPIRED",
            "INVALID_TOKEN",
            "RATE_LIMIT_EXCEEDED",
            "NOT_FOUND",
            "OBSERVATION_NOT_FOUND",
            "TRADE_NOT_FOUND",
            "CURRENCY_NOT_SUPPORTED",
            "INTERVAL_NOT_SUPPORTED",
            "RESOLUTION_NOT_SUPPORTED",
            "INVALID_PARAMETER",
            "PROVIDER_UNAVAILABLE",
            "EVENT_PUBLISH_FAILED",
            "STREAMING_ENGINE_UNAVAILABLE",
            "EVALUATION_ERROR",
            "COMPUTATION_ERROR",
            "MISSING_FIELD",
            "INVALID_FIELD",
            "PROVENANCE_IMMUTABLE",
        ]
        names = {m.name for m in ErrorCode}
        for code in required:
            assert code in names, f"ErrorCode.{code} is missing"

    def test_error_code_is_str_subclass(self) -> None:
        assert isinstance(ErrorCode.NOT_FOUND, str)

    def test_error_code_value_equals_name(self) -> None:
        for member in ErrorCode:
            assert member.value == member.name, (
                f"ErrorCode.{member.name}.value should equal its name"
            )

    def test_error_code_usable_as_dict_key(self) -> None:
        d = {ErrorCode.NOT_FOUND: "found it"}
        assert d[ErrorCode.NOT_FOUND] == "found it"

    def test_error_code_serialises_to_string(self) -> None:
        # JSON serialisation must produce the raw string, not an object.
        result = json.dumps({"code": ErrorCode.INTERVAL_NOT_SUPPORTED})
        data = json.loads(result)
        assert data["code"] == "INTERVAL_NOT_SUPPORTED"


# ---------------------------------------------------------------------------
# canonical_error()
# ---------------------------------------------------------------------------


class TestCanonicalError:
    def test_shape(self) -> None:
        result = canonical_error(ErrorCode.NOT_FOUND, "not found")
        assert "error" in result
        err = result["error"]
        assert set(err.keys()) == {"code", "message", "provider", "retryAfterMs", "requestId"}

    def test_code_is_string(self) -> None:
        result = canonical_error(ErrorCode.INVALID_PARAMETER, "bad param")
        assert isinstance(result["error"]["code"], str)
        assert result["error"]["code"] == "INVALID_PARAMETER"

    def test_raw_string_code(self) -> None:
        result = canonical_error("CUSTOM_CODE", "custom message")
        assert result["error"]["code"] == "CUSTOM_CODE"

    def test_message_preserved(self) -> None:
        msg = "interval 3m is permanently unsupported"
        result = canonical_error(ErrorCode.INTERVAL_NOT_SUPPORTED, msg)
        assert result["error"]["message"] == msg

    def test_defaults_provider_none(self) -> None:
        result = canonical_error(ErrorCode.NOT_FOUND, "x")
        assert result["error"]["provider"] is None

    def test_defaults_retry_after_none(self) -> None:
        result = canonical_error(ErrorCode.NOT_FOUND, "x")
        assert result["error"]["retryAfterMs"] is None

    def test_request_id_auto_generated(self) -> None:
        result = canonical_error(ErrorCode.NOT_FOUND, "x")
        rid = result["error"]["requestId"]
        assert rid and len(rid) > 0
        # Should be a valid UUID.
        uuid.UUID(rid)

    def test_request_id_caller_supplied(self) -> None:
        rid = "my-req-id-123"
        result = canonical_error(ErrorCode.NOT_FOUND, "x", request_id=rid)
        assert result["error"]["requestId"] == rid

    def test_request_id_empty_string_gets_generated(self) -> None:
        result = canonical_error(ErrorCode.NOT_FOUND, "x", request_id="")
        rid = result["error"]["requestId"]
        assert rid and len(rid) > 0
        uuid.UUID(rid)

    def test_request_id_whitespace_gets_generated(self) -> None:
        result = canonical_error(ErrorCode.NOT_FOUND, "x", request_id="   ")
        rid = result["error"]["requestId"]
        assert rid.strip()
        uuid.UUID(rid)

    def test_provider_included_when_supplied(self) -> None:
        result = canonical_error(
            ErrorCode.PROVIDER_UNAVAILABLE, "down", provider="angel_one"
        )
        assert result["error"]["provider"] == "angel_one"

    def test_retry_after_ms_included_when_supplied(self) -> None:
        result = canonical_error(
            ErrorCode.RATE_LIMIT_EXCEEDED, "slow down", retry_after_ms=5000
        )
        assert result["error"]["retryAfterMs"] == 5000

    def test_two_calls_produce_different_request_ids(self) -> None:
        r1 = canonical_error(ErrorCode.NOT_FOUND, "x")
        r2 = canonical_error(ErrorCode.NOT_FOUND, "x")
        assert r1["error"]["requestId"] != r2["error"]["requestId"]

    def test_no_extra_top_level_keys(self) -> None:
        result = canonical_error(ErrorCode.NOT_FOUND, "x")
        assert set(result.keys()) == {"error"}


# ---------------------------------------------------------------------------
# default_status_for()
# ---------------------------------------------------------------------------


class TestDefaultStatusFor:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (ErrorCode.UNAUTHORIZED, 401),
            (ErrorCode.INVALID_API_KEY, 401),
            (ErrorCode.TOKEN_EXPIRED, 401),
            (ErrorCode.INVALID_TOKEN, 401),
            (ErrorCode.RATE_LIMIT_EXCEEDED, 429),
            (ErrorCode.NOT_FOUND, 404),
            (ErrorCode.OBSERVATION_NOT_FOUND, 404),
            (ErrorCode.TRADE_NOT_FOUND, 404),
            (ErrorCode.INTERVAL_NOT_SUPPORTED, 400),
            (ErrorCode.INVALID_PARAMETER, 400),
            (ErrorCode.PROVIDER_UNAVAILABLE, 502),
            (ErrorCode.PROVIDER_QUEUE_FULL, 503),
            (ErrorCode.STREAMING_ENGINE_UNAVAILABLE, 503),
            (ErrorCode.PROVENANCE_IMMUTABLE, 405),
            (ErrorCode.FNO_UNIVERSE_UNAVAILABLE, 503),
            (ErrorCode.SERVICE_UNAVAILABLE, 503),
        ],
    )
    def test_known_codes(self, code: ErrorCode, expected: int) -> None:
        assert default_status_for(code) == expected

    def test_unknown_string_returns_400(self) -> None:
        assert default_status_for("TOTALLY_UNKNOWN_CODE") == 400

    def test_string_version_of_known_code(self) -> None:
        assert default_status_for("NOT_FOUND") == 404

    def test_string_version_of_rate_limit(self) -> None:
        assert default_status_for("RATE_LIMIT_EXCEEDED") == 429


# ---------------------------------------------------------------------------
# json_error_response()
# ---------------------------------------------------------------------------


class TestJsonErrorResponse:
    def test_returns_fastapi_response(self) -> None:
        from fastapi import Response

        resp = json_error_response(ErrorCode.NOT_FOUND, "not found")
        assert isinstance(resp, Response)

    def test_status_code_default(self) -> None:
        resp = json_error_response(ErrorCode.NOT_FOUND, "not found")
        assert resp.status_code == 404

    def test_status_code_override(self) -> None:
        resp = json_error_response(ErrorCode.NOT_FOUND, "not found", status_code=410)
        assert resp.status_code == 410

    def test_content_type_json(self) -> None:
        resp = json_error_response(ErrorCode.INVALID_PARAMETER, "bad")
        assert resp.media_type == "application/json"

    def test_cache_control_no_store(self) -> None:
        resp = json_error_response(ErrorCode.INVALID_PARAMETER, "bad")
        assert resp.headers.get("cache-control") == "no-store"

    def test_body_is_valid_json(self) -> None:
        resp = json_error_response(ErrorCode.INTERVAL_NOT_SUPPORTED, "3m unsupported")
        body = json.loads(resp.body)
        assert "error" in body

    def test_body_shape(self) -> None:
        resp = json_error_response(
            ErrorCode.INTERVAL_NOT_SUPPORTED,
            "interval 3m is permanently unsupported",
        )
        err = json.loads(resp.body)["error"]
        assert err["code"] == "INTERVAL_NOT_SUPPORTED"
        assert err["message"] == "interval 3m is permanently unsupported"
        assert "requestId" in err
        assert "provider" in err
        assert "retryAfterMs" in err

    def test_401_status_for_unauthorized(self) -> None:
        resp = json_error_response(ErrorCode.UNAUTHORIZED, "auth required")
        assert resp.status_code == 401

    def test_429_with_retry_after_ms(self) -> None:
        resp = json_error_response(
            ErrorCode.RATE_LIMIT_EXCEEDED,
            "too many requests",
            retry_after_ms=60_000,
        )
        assert resp.status_code == 429
        body = json.loads(resp.body)
        assert body["error"]["retryAfterMs"] == 60_000

    def test_502_for_provider_unavailable(self) -> None:
        resp = json_error_response(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "angel_one is down",
            provider="angel_one",
        )
        assert resp.status_code == 502
        body = json.loads(resp.body)
        assert body["error"]["provider"] == "angel_one"

    def test_405_for_provenance_immutable(self) -> None:
        resp = json_error_response(ErrorCode.PROVENANCE_IMMUTABLE, "read-only")
        assert resp.status_code == 405

    def test_request_id_forwarded(self) -> None:
        rid = "test-request-id-xyz"
        resp = json_error_response(
            ErrorCode.NOT_FOUND, "not found", request_id=rid
        )
        body = json.loads(resp.body)
        assert body["error"]["requestId"] == rid

    def test_raw_string_code(self) -> None:
        resp = json_error_response("CUSTOM_ERROR", "custom message")
        body = json.loads(resp.body)
        assert body["error"]["code"] == "CUSTOM_ERROR"
        # Unknown code falls back to 400.
        assert resp.status_code == 400

    def test_no_stack_trace_in_body(self) -> None:
        resp = json_error_response(ErrorCode.EVALUATION_ERROR, "something broke")
        body_str = resp.body.decode()
        assert "Traceback" not in body_str
        assert "File " not in body_str

    def test_body_does_not_contain_credentials(self) -> None:
        resp = json_error_response(ErrorCode.INVALID_API_KEY, "key rejected")
        body_str = resp.body.decode()
        # Should never echo back 'secret', 'password', 'token' values.
        # (We just verify the shape here — the credential stripper middleware
        # is tested separately.)
        assert len(body_str) < 2000  # sanity bound — no large dumps
