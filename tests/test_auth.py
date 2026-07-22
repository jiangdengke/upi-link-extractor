from upi_link.auth import AdminAuth, LoginRateLimiter


def test_admin_auth_password_and_signed_session() -> None:
    auth = AdminAuth("correct-password", "stable-session-secret", ttl_seconds=60)
    assert auth.enabled is True
    assert auth.verify_password("correct-password") is True
    assert auth.verify_password("wrong") is False

    token = auth.issue()
    assert auth.verify(token) is True
    assert auth.verify(token + "tampered") is False


def test_login_rate_limiter_blocks_after_failures() -> None:
    limiter = LoginRateLimiter(max_failures=2, window_seconds=60)
    assert limiter.allowed("client") is True
    limiter.record_failure("client")
    limiter.record_failure("client")
    assert limiter.allowed("client") is False
    assert limiter.retry_after("client") > 0
    limiter.clear("client")
    assert limiter.allowed("client") is True
