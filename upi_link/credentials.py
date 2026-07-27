from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any


class CredentialError(ValueError):
    """Raised when an access token or account email cannot be resolved."""


@dataclass(frozen=True)
class Credential:
    access_token: str = field(repr=False)
    email: str


_TOKEN_KEYS = ("accessToken", "access_token", "token")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*)?")


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        value = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _find_email(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("email", "email_address"):
            candidate = str(value.get(key) or "").strip()
            if _EMAIL_RE.match(candidate):
                return candidate
        for nested in value.values():
            candidate = _find_email(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _find_email(nested)
            if candidate:
                return candidate
    return ""


def _parse_json(raw: str) -> tuple[str, str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(value, dict):
        return "", ""
    token = ""
    for key in _TOKEN_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            token = candidate.strip()
            break
    return token, _find_email(value)


def parse_credential(
    raw: str,
    email_hint: str = "",
    *,
    require_email: bool = True,
) -> Credential:
    """Parse a raw access token or a JSON object containing ``accessToken``.

    Session cookies are intentionally not exchanged here. The caller must
    provide an access token obtained from an account it is authorized to use.
    """

    text = str(raw or "").strip()
    if not text:
        raise CredentialError("请填写 Access Token 或包含 accessToken 的 JSON")

    if text.lower().startswith("bearer "):
        text = text[7:].strip()

    token, json_email = _parse_json(text)
    if not token:
        match = _JWT_RE.search(text)
        token = match.group(0) if match else text
    token = token.strip().strip('"').strip("'")

    if len(token) < 40 or any(ch.isspace() for ch in token):
        raise CredentialError("未识别到有效 Access Token；当前项目不接收 Session Cookie")

    jwt_email = _find_email(_decode_jwt_payload(token))
    email = str(email_hint or "").strip() or json_email or jwt_email
    if require_email and not _EMAIL_RE.match(email):
        raise CredentialError("无法从 Token 解析邮箱，请在“账号邮箱”中手动填写")
    if email and not _EMAIL_RE.match(email):
        raise CredentialError("账号邮箱格式无效")

    return Credential(access_token=token, email=email)


def redact_sensitive(text: str, *secrets: str) -> str:
    safe = str(text or "")
    for secret in secrets:
        if secret and len(secret) >= 8:
            safe = safe.replace(secret, "[REDACTED]")
    safe = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", safe)
    safe = _JWT_RE.sub("[REDACTED_JWT]", safe)
    return safe
