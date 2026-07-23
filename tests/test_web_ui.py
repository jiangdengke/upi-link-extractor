from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_result_actions_and_time_labels_exist() -> None:
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    for label in (
        "复制长链",
        "打开长链",
        "打开二维码",
        "长链生成时间",
        "链接过期时间",
    ):
        assert label in app_js


def test_qr_modal_exists() -> None:
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="qr-modal"' in index
    assert 'id="qr-open-new"' in index


def test_cdk_user_and_admin_interfaces_exist() -> None:
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    admin = (ROOT / "static" / "admin.html").read_text(encoding="utf-8")
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    admin_js = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")
    assert 'id="cdk"' in index
    assert "检测 / 兑换" in index
    assert "CDK 管理登录" in admin
    assert "/api/cdk/verify" in app_js
    assert "/api/admin/cdks" in admin_js


def test_proxy_configuration_is_admin_only() -> None:
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    admin = (ROOT / "static" / "admin.html").read_text(encoding="utf-8")
    admin_js = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")
    assert 'id="proxy-pool"' not in index
    assert 'id="login-proxy"' not in index
    assert "proxy_pool: data.get" not in app_js
    assert 'id="settings-proxy-pool"' in admin
    assert "/api/admin/settings" in admin_js


def test_user_page_contains_no_admin_entry_or_admin_copy() -> None:
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'href="/admin"' not in index
    assert "管理端" not in index
    assert "管理员" not in index
    assert "proxy_count" not in app_js


def test_foarge_admin_controls_and_user_payment_progress_exist() -> None:
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    admin = (ROOT / "static" / "admin.html").read_text(encoding="utf-8")
    admin_js = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="foarge-cdks"' in admin
    assert "每行一个" in admin
    assert 'value="foarge"' in admin
    assert "/api/admin/foarge/check" in admin_js
    assert "PAYMENT_STAGES" in app_js
    assert "支付进度" in app_js
    assert "X-Publisher" not in app_js
    assert "LOCAL ONLY" not in index
    assert "提交至支付服务用于验单" in index


def test_qr_response_is_inline() -> None:
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "filename=path.name" not in main
