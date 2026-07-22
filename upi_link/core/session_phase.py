"""Lightweight session shim for the ported UPI runner.

The upstream ``gpt-upi-tool`` shipped a full pure-HTTP / browser login stack in
``session_phase.py``. In this project we do not port that stack: the UPI link
extractor always injects a ``login_fn`` (see ``login.py``) that reuses the host
project's existing credential store / token-refresh / OAuth login to obtain a
ChatGPT ``accessToken``.

This module only re-exports the small surface that ``upi_runner`` still imports:

    - ``SessionError``          raised by ``login_fn`` on failure
    - ``is_fatal_login_error``  retry gate used by the runner's login loop
    - ``get_session_pure_request``  fallback path (never used when login_fn set)
"""
from __future__ import annotations

from typing import Any


class SessionError(Exception):
    """Login/session fetch failed."""


# Fatal (non-retryable) login errors — keep in sync with the upstream list so
# the runner's retry gate behaves the same. login_fn should raise SessionError
# whose message contains one of these markers when the failure is permanent.
NON_RETRYABLE_LOGIN_PATTERNS: tuple[str, ...] = (
    "password verify failed",
    "mfa verify failed",
    "no mail_provider available",
    "no secret provided",
    "yêu cầu 2fa nhưng không có",
    "otp polling returned empty",
    "passwordless otp login but no mail_provider",
    "no access token",
    "no credentials",
    "login not supported",
)


def is_fatal_login_error(exc: BaseException | str) -> bool:
    """True if the login error is permanent (do not retry)."""
    msg = exc if isinstance(exc, str) else str(exc)
    lower = msg.lower()
    return any(pat in lower for pat in NON_RETRYABLE_LOGIN_PATTERNS)


async def get_session_pure_request(**_kwargs: Any) -> dict[str, Any]:
    """Fallback login path — intentionally unsupported here.

    The UPI extractor always provides ``login_fn``; if it does not, the caller
    misconfigured the adapter. Raise a fatal SessionError so the runner stops
    immediately instead of silently spinning.
    """
    raise SessionError(
        "login not supported: UPI extractor must inject login_fn "
        "(no built-in pure-HTTP login in this project)"
    )
