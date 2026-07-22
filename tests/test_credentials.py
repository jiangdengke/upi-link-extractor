from __future__ import annotations

import base64
import json

import pytest

from upi_link.credentials import CredentialError, parse_credential, redact_sensitive


def _segment(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _token(payload: dict) -> str:
    return f"{_segment({'alg': 'none'})}.{_segment(payload)}.signature"


def test_parse_raw_jwt_extracts_email() -> None:
    token = _token({"email": "owner@example.com"})
    credential = parse_credential(token)
    assert credential.email == "owner@example.com"
    assert credential.access_token == token


def test_parse_json_access_token() -> None:
    token = _token({"sub": "user-1"})
    credential = parse_credential(
        json.dumps({"accessToken": token, "user": {"email": "json@example.com"}})
    )
    assert credential.email == "json@example.com"


def test_email_hint_is_used_when_token_has_no_email() -> None:
    token = _token({"sub": "user-1"})
    assert parse_credential(token, "hint@example.com").email == "hint@example.com"


def test_missing_email_is_rejected() -> None:
    with pytest.raises(CredentialError):
        parse_credential(_token({"sub": "user-1"}))


def test_redaction_removes_exact_and_bearer_tokens() -> None:
    token = _token({"email": "owner@example.com"})
    safe = redact_sensitive(f"Authorization: Bearer {token}; raw={token}", token)
    assert token not in safe
    assert "REDACTED" in safe

