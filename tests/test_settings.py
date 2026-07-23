import pytest

from upi_link.settings import SettingsStore


def test_settings_persist_and_hide_proxy_details_from_public_status(tmp_path) -> None:
    path = tmp_path / "settings.db"
    store = SettingsStore(path)
    assert store.get()["proxy_pool"] == []

    saved = store.update(
        proxy_pool="http://one:pass@proxy.example:2000\nhttp://two:pass@proxy.example:2000",
        login_proxy="http://login:pass@login.example:2000",
        approve_retries=40,
        approve_concurrency=5,
        proxy_from_step=3,
    )
    assert len(saved["proxy_pool"]) == 2

    restarted = SettingsStore(path)
    assert restarted.get()["approve_concurrency"] == 5
    public = restarted.public_status()
    assert public["proxy_count"] == 2
    assert "proxy_pool" not in public
    assert "login_proxy" not in public


def test_settings_reject_more_than_100_proxies(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.db")
    with pytest.raises(ValueError, match="代理数量不能超过 100"):
        store.update(
            proxy_pool="\n".join(f"http://proxy-{index}:2000" for index in range(101)),
            login_proxy="",
            approve_retries=30,
            approve_concurrency=1,
            proxy_from_step=3,
        )


def test_foarge_cdk_is_persisted_and_only_exposed_masked(tmp_path) -> None:
    path = tmp_path / "foarge.db"
    store = SettingsStore(path)
    assert store.foarge_status() == {
        "configured": False,
        "masked_cdk": "",
        "updated_at": 0,
    }

    status = store.update_foarge(cdk="pbk-abcd-efgh-ijkl")
    assert status["configured"] is True
    assert status["masked_cdk"] == "PBK-****IJKL"
    assert "PBK-ABCD-EFGH-IJKL" not in repr(status)

    restarted = SettingsStore(path)
    assert restarted.get_foarge()["cdk"] == "PBK-ABCD-EFGH-IJKL"
    restarted.update_foarge(clear=True)
    assert restarted.foarge_status()["configured"] is False

    with pytest.raises(ValueError, match="PBK-"):
        restarted.update_foarge(cdk="invalid")
