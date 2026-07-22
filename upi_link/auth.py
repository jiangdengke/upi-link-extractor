from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque


class AdminAuth:
    def __init__(
        self,
        password: str,
        session_secret: str = "",
        *,
        ttl_seconds: int = 12 * 60 * 60,
    ) -> None:
        self._password = str(password or "")
        self._secret = (
            session_secret.encode("utf-8")
            if session_secret
            else secrets.token_bytes(32)
        )
        self.ttl_seconds = ttl_seconds

    @property
    def enabled(self) -> bool:
        return bool(self._password)

    def verify_password(self, candidate: str) -> bool:
        if not self.enabled:
            return False
        return hmac.compare_digest(
            self._password.encode("utf-8"),
            str(candidate or "").encode("utf-8"),
        )

    def issue(self) -> str:
        payload = {
            "role": "admin",
            "exp": int(time.time()) + self.ttl_seconds,
            "nonce": secrets.token_hex(8),
        }
        encoded = _b64_encode(json.dumps(payload, separators=(",", ":")).encode())
        signature = hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest()
        return f"{encoded}.{_b64_encode(signature)}"

    def verify(self, token: str) -> bool:
        try:
            encoded, supplied_signature = str(token or "").split(".", 1)
            expected = hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(_b64_decode(supplied_signature), expected):
                return False
            payload = json.loads(_b64_decode(encoded).decode("utf-8"))
            return payload.get("role") == "admin" and int(payload.get("exp", 0)) > time.time()
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            return False


class LoginRateLimiter:
    def __init__(self, *, max_failures: int = 5, window_seconds: int = 600) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def allowed(self, key: str) -> bool:
        failures = self._active_failures(key)
        return len(failures) < self.max_failures

    def record_failure(self, key: str) -> None:
        failures = self._active_failures(key)
        failures.append(time.time())

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)

    def retry_after(self, key: str) -> int:
        failures = self._active_failures(key)
        if len(failures) < self.max_failures:
            return 0
        return max(1, int(self.window_seconds - (time.time() - failures[0])))

    def _active_failures(self, key: str) -> deque[float]:
        failures = self._failures[key]
        cutoff = time.time() - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        return failures


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
