import autoteam.ipv6_pool as ipv6_pool_mod
from autoteam import config, manager


def test_ipv6_pool_status_reports_preflight_errors(monkeypatch, tmp_path):
    pool = ipv6_pool_mod.IPv6Pool()
    monkeypatch.setattr(config, "AUTOTEAM_IPV6_POOL_ENABLED", True)
    monkeypatch.setattr(config, "AUTOTEAM_IPV6_POOL_REQUIRED", True)
    monkeypatch.setattr(config, "IPV6_PREFIX", "")
    monkeypatch.setattr(config, "IPV6_IFACE", "eth0")
    monkeypatch.setattr(config, "IPV6_PROXY_PORT_START", 30000)
    monkeypatch.setattr(config, "IPV6_PROXY_PORT_END", 30010)
    monkeypatch.setattr(config, "IPV6_PROXY_POOL_FILE", str(tmp_path / "ipv6_pool.json"))
    monkeypatch.setattr(ipv6_pool_mod.shutil, "which", lambda _command: "/usr/sbin/ip")

    status = pool.status()

    assert status["enabled"] is False
    assert status["required"] is True
    assert status["ok"] is False
    assert status["count"] == 0
    assert status["preflight"]["prefix_configured"] is False
    assert "ipv6_required_but_disabled" in status["preflight"]["errors"]


def test_ipv6_pool_status_reports_port_usage(monkeypatch, tmp_path):
    pool = ipv6_pool_mod.IPv6Pool()
    monkeypatch.setattr(config, "AUTOTEAM_IPV6_POOL_ENABLED", True)
    monkeypatch.setattr(config, "AUTOTEAM_IPV6_POOL_REQUIRED", False)
    monkeypatch.setattr(config, "IPV6_PREFIX", "2001:db8::/64")
    monkeypatch.setattr(config, "IPV6_IFACE", "eth0")
    monkeypatch.setattr(config, "IPV6_PROXY_PORT_START", 30000)
    monkeypatch.setattr(config, "IPV6_PROXY_PORT_END", 30001)
    monkeypatch.setattr(config, "IPV6_PROXY_POOL_FILE", str(tmp_path / "ipv6_pool.json"))
    monkeypatch.setattr(ipv6_pool_mod.shutil, "which", lambda _command: "/usr/sbin/ip")
    pool._used_ports.add(30000)

    status = pool.status()

    assert status["enabled"] is True
    assert status["ok"] is True
    assert status["used_ports"] == 1
    assert status["port_capacity"] == 2
    assert status["port_usage_ratio"] == 0.5


def test_required_ipv6_pool_failure_does_not_fall_back_to_direct(monkeypatch):
    monkeypatch.setattr(config, "AUTOTEAM_IPV6_POOL_REQUIRED", True)
    monkeypatch.setattr(
        "autoteam.ipv6_pool.ipv6_pool.ensure",
        lambda _email: (_ for _ in ()).throw(RuntimeError("proxy unavailable")),
    )

    try:
        manager._ensure_account_ipv6_proxy("child@example.com")
    except RuntimeError as exc:
        assert "proxy unavailable" in str(exc)
    else:
        raise AssertionError("required IPv6 pool failure must not fall back to direct")
