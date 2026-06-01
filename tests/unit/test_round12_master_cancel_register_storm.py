"""Round 12 task 06-01 — master 订阅误判 / 注册风暴 / 重试延迟修复回归。

覆盖:
  R2  — codex_auth._build_auth_url 默认 prompt=login(+ 两个新参数),env 可回退 consent。
  R1b — master_health.invalidate_cache(account 维度 + 全清)。

(R1 三字段判定的 active/cancelled 回归见 test_master_subscription_probe.py 与
 test_round11_master_health_grace.py;此文件聚焦本轮新增的 R2/R1b 入口。)
"""
from __future__ import annotations

import urllib.parse

import pytest


def _auth_url_params(monkeypatch=None) -> dict:
    from autoteam.codex_auth import _build_auth_url

    url = _build_auth_url("test-code-challenge", "test-state")
    qs = urllib.parse.urlparse(url).query
    return dict(urllib.parse.parse_qsl(qs))


# ---------------------------------------------------------------------------
# R2 — _build_auth_url prompt=login 对齐 autoteam-1
# ---------------------------------------------------------------------------


def test_build_auth_url_default_prompt_is_login(monkeypatch):
    """默认(无 env)prompt=login,而非旧的 consent —— add-phone 风暴的直接修复。"""
    monkeypatch.delenv("CODEX_AUTHORIZE_PROMPT", raising=False)
    params = _auth_url_params()
    assert params.get("prompt") == "login"


def test_build_auth_url_includes_new_params(monkeypatch):
    """对齐 autoteam-1/CPA:新增 id_token_add_organizations + codex_cli_simplified_flow。"""
    monkeypatch.delenv("CODEX_AUTHORIZE_PROMPT", raising=False)
    params = _auth_url_params()
    assert params.get("id_token_add_organizations") == "true"
    assert params.get("codex_cli_simplified_flow") == "true"
    # 基础参数不丢
    assert params.get("response_type") == "code"
    assert params.get("code_challenge_method") == "S256"


def test_build_auth_url_env_can_fallback_to_consent(monkeypatch):
    """env CODEX_AUTHORIZE_PROMPT=consent 可回退旧行为(应急开关)。"""
    monkeypatch.setenv("CODEX_AUTHORIZE_PROMPT", "consent")
    params = _auth_url_params()
    assert params.get("prompt") == "consent"


def test_build_auth_url_env_empty_defaults_login(monkeypatch):
    """env 设为空串时仍回到 login(get_codex_authorize_prompt 兜底)。"""
    monkeypatch.setenv("CODEX_AUTHORIZE_PROMPT", "")
    params = _auth_url_params()
    assert params.get("prompt") == "login"


# ---------------------------------------------------------------------------
# R1b — invalidate_cache(admin 重登后清母号 health cache,防陈旧/误判锁定)
# ---------------------------------------------------------------------------


def _seed_cache(monkeypatch, tmp_path, entries: dict):
    import autoteam.master_health as mh

    fake_cache = tmp_path / "master_health_cache.json"
    monkeypatch.setattr(mh, "CACHE_FILE", fake_cache)
    monkeypatch.setattr(mh, "ACCOUNTS_DIR", tmp_path)
    mh._save_cache({"schema_version": mh.CACHE_SCHEMA_VERSION, "cache": entries})
    return mh


def test_invalidate_cache_removes_single_account(monkeypatch, tmp_path):
    mh = _seed_cache(monkeypatch, tmp_path, {
        "acc-A": {"healthy": True, "reason": "active", "probed_at": 1.0},
        "acc-B": {"healthy": True, "reason": "active", "probed_at": 1.0},
    })
    mh.invalidate_cache("acc-A")
    data = mh._load_cache()
    assert "acc-A" not in data["cache"]
    assert "acc-B" in data["cache"]  # 只删指定 account,不误伤其他母号


def test_invalidate_cache_none_clears_all(monkeypatch, tmp_path):
    mh = _seed_cache(monkeypatch, tmp_path, {
        "acc-A": {"healthy": True, "reason": "active", "probed_at": 1.0},
        "acc-B": {"healthy": False, "reason": "subscription_cancelled", "probed_at": 1.0},
    })
    mh.invalidate_cache(None)
    data = mh._load_cache()
    assert data["cache"] == {}


def test_invalidate_cache_missing_account_is_noop(monkeypatch, tmp_path):
    mh = _seed_cache(monkeypatch, tmp_path, {
        "acc-A": {"healthy": True, "reason": "active", "probed_at": 1.0},
    })
    mh.invalidate_cache("does-not-exist")  # 不抛
    data = mh._load_cache()
    assert "acc-A" in data["cache"]


def test_invalidate_cache_never_raises(monkeypatch, tmp_path):
    """M-I1 守恒延伸:任何异常都被吞,不向上传播。"""
    import autoteam.master_health as mh

    def _boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(mh, "_load_cache", _boom)
    # 不应抛
    mh.invalidate_cache("acc-A")
    mh.invalidate_cache(None)


# ---------------------------------------------------------------------------
# R4 — _call_login 不把 RegisterBlocked 吞成 generic "exception"
# ---------------------------------------------------------------------------


def test_login_result_maps_add_phone_and_breaks_immediately(monkeypatch):
    """RegisterBlocked(is_phone) → error_type=add_phone + retryable=False,attempt loop 立即 break。"""
    import autoteam.manager as mgr
    from autoteam.invite import RegisterBlocked

    calls = {"n": 0}

    def _raise_phone(*args, **kwargs):
        calls["n"] += 1
        raise RegisterBlocked("oauth_consent_1", "add-phone 手机验证", is_phone=True)

    monkeypatch.setattr(mgr, "login_codex_via_browser", _raise_phone)

    result = mgr._login_codex_with_result("a@x.com", "pw", max_attempts=3)
    assert result["error_type"] == "add_phone"
    assert result["retryable"] is False
    assert result["attempts"] == 1          # 没白跑 3 次
    assert calls["n"] == 1                   # 浏览器只开了一次


def test_login_result_maps_duplicate_and_human_verification(monkeypatch):
    import autoteam.manager as mgr
    from autoteam.invite import RegisterBlocked

    def _raise_dup(*a, **k):
        raise RegisterBlocked("email", "duplicate email", is_duplicate=True)

    monkeypatch.setattr(mgr, "login_codex_via_browser", _raise_dup)
    r = mgr._login_codex_with_result("a@x.com", "pw", max_attempts=3)
    assert r["error_type"] == "duplicate_email"
    assert r["retryable"] is False

    def _raise_other(*a, **k):
        raise RegisterBlocked("captcha", "human verification")

    monkeypatch.setattr(mgr, "login_codex_via_browser", _raise_other)
    r2 = mgr._login_codex_with_result("a@x.com", "pw", max_attempts=3)
    assert r2["error_type"] == "human_verification"
    assert r2["retryable"] is False


def test_generic_exception_still_maps_to_exception(monkeypatch):
    """非 RegisterBlocked 的异常仍归 exception + retryable=True(行为不变)。"""
    import autoteam.manager as mgr

    def _boom(*a, **k):
        raise RuntimeError("network blip")

    monkeypatch.setattr(mgr, "login_codex_via_browser", _boom)
    r = mgr._login_codex_with_result("a@x.com", "pw", max_attempts=2)
    assert r["error_type"] == "exception"
    assert r["retryable"] is True


# ---------------------------------------------------------------------------
# R5 — 退避 24h 硬封顶(360018 分钟症状的根治)
# ---------------------------------------------------------------------------


def test_clamp_retry_delay_bounds():
    from autoteam.manager import _AUTH_RETRY_DELAY_MAX_SECONDS, _clamp_retry_delay

    assert _clamp_retry_delay(10) == 60                       # 下界
    assert _clamp_retry_delay(10**9) == _AUTH_RETRY_DELAY_MAX_SECONDS  # 上界 24h
    assert _clamp_retry_delay(3600) == 3600                   # 区间内原样
    assert _clamp_retry_delay("bad") == 60                    # 非法值收敛
    assert _clamp_retry_delay(None) == 60


def test_retry_delays_clamped_when_interval_polluted(monkeypatch):
    """interval 被污染成 ~3.6M 秒(360018min 的干净来源)→ 每档 ≤ 24h,不再爆。"""
    import autoteam.api as api
    from autoteam.manager import _AUTH_RETRY_DELAY_MAX_SECONDS, _auth_repair_retry_delays

    monkeypatch.setitem(api._auto_check_config, "interval", 3_600_180)
    delays = _auth_repair_retry_delays()
    assert len(delays) == 3
    assert all(d <= _AUTH_RETRY_DELAY_MAX_SECONDS for d in delays)
    assert max(delays) == _AUTH_RETRY_DELAY_MAX_SECONDS


def test_add_phone_delays_clamped_for_large_max_retries(monkeypatch):
    """max_retries 调到 15 → 1800×2^14 本会爆,封顶后每档 ≤ 24h。"""
    import autoteam.api as api
    from autoteam.manager import (
        _AUTH_RETRY_DELAY_MAX_SECONDS,
        _auth_repair_add_phone_retry_delays,
    )

    monkeypatch.setitem(api._auto_check_config, "interval", 1800)
    delays = _auth_repair_add_phone_retry_delays(15)
    assert len(delays) == 15
    assert all(d <= _AUTH_RETRY_DELAY_MAX_SECONDS for d in delays)
    assert max(delays) == _AUTH_RETRY_DELAY_MAX_SECONDS


def test_set_auto_check_config_clamps_interval(monkeypatch):
    """UI/API 写入超大 interval 被入口 ceiling 收敛到 24h(防 retry_after 爆)。"""
    import autoteam.api as api

    prev = dict(api._auto_check_config)
    try:
        cfg = api.AutoCheckConfig(interval=10**9, target_seats=1, threshold=10, min_low=1)
        out = api.set_auto_check_config(cfg)
        assert out["interval"] == api._AUTO_CHECK_INTERVAL_MAX
    finally:
        api._auto_check_config.update(prev)


# ---------------------------------------------------------------------------
# R3 — create_new_account 前置闸:master cancelled 时不建邮箱不开浏览器
# ---------------------------------------------------------------------------


def test_create_new_account_short_circuits_when_master_cancelled(monkeypatch):
    """master cancelled(cache)→ create_new_account 在任何邮箱/浏览器开销前 fail-fast。"""
    import autoteam.manager as mgr
    import autoteam.master_health as mh

    monkeypatch.setattr(mgr, "_chatgpt_session_ready", lambda api: True)
    monkeypatch.setattr(
        mh, "is_master_subscription_healthy",
        lambda *a, **k: (False, "subscription_cancelled",
                         {"account_id": "master-x", "cache_hit": True, "current_user_role": "account-owner"}),
    )

    def _must_not_run(*a, **k):
        raise AssertionError("master cancelled 时下游(建邮箱/注册)绝不能执行")

    # 闸在这三者之前返回 → 它们都不该被触达
    monkeypatch.setattr(mgr, "_resolve_mail_client_or_default", _must_not_run)
    monkeypatch.setattr(mgr, "create_account_direct", _must_not_run)
    monkeypatch.setattr(mgr, "_check_pending_invites", _must_not_run)

    recorded = {}
    monkeypatch.setattr(
        mgr, "record_failure",
        lambda email, category, reason="", **extra: recorded.update(
            category=category, stage=extra.get("stage")),
    )

    out = {}
    result = mgr.create_new_account(object(), mail_client=object(), out_outcome=out)
    assert result is None
    assert out["status"] == "master_degraded"
    assert recorded["category"] == "master_subscription_degraded"
    assert recorded["stage"] == "create_new_account_pre_register_gate"


def test_create_new_account_does_not_block_on_network_error(monkeypatch):
    """probe network_error/不确定 → 放行(只拦 subscription_cancelled),交注册后兜底闸。"""
    import autoteam.manager as mgr
    import autoteam.master_health as mh

    monkeypatch.setattr(mgr, "_chatgpt_session_ready", lambda api: True)
    monkeypatch.setattr(
        mh, "is_master_subscription_healthy",
        lambda *a, **k: (False, "network_error", {}),
    )

    class _PastGate(Exception):
        pass

    def _past_gate(*a, **k):
        raise _PastGate()

    # 闸放行后第一站是 _resolve_mail_client_or_default → 抛 sentinel 证明已过闸
    monkeypatch.setattr(mgr, "_resolve_mail_client_or_default", _past_gate)

    with pytest.raises(_PastGate):
        mgr.create_new_account(object(), mail_client=object())


# ---------------------------------------------------------------------------
# R6 — consent loop 自恢复接线(静态:round-12 无账号无法 e2e,核验 helper 已被接进循环)
# ---------------------------------------------------------------------------


def test_consent_loop_wires_self_recovery_helpers():
    """login_codex_via_browser 的 consent loop 必须接入三类站内自恢复 helper。

    这些 helper 此前已定义但未在循环中被调用(dead code);R6 把它们接进 consent loop。
    静态断言其调用点存在,防止回归到"定义了却不用"。
    """
    import inspect

    import autoteam.codex_auth as cx

    src = inspect.getsource(cx.login_codex_via_browser)
    assert "_recover_oauth_timeout_page(page)" in src
    assert "_recover_oauth_no_valid_organizations_page(page)" in src
    assert "_is_oauth_login_challenge_page(page)" in src
    assert "_complete_oauth_login_challenge(" in src
    # 计数器存在(各自恢复有上限,不会无限循环)
    assert "oauth_timeout_recoveries" in src
    assert "no_valid_org_recoveries" in src
    assert "login_challenge_recoveries" in src


def test_recovery_helpers_are_noop_on_normal_page():
    """守卫:正常 consent 页(无超时/no_valid_org/login 文案)→ 三 helper 都返回 False(不误触发)。"""
    import autoteam.codex_auth as cx

    class _Page:
        url = "https://auth.openai.com/oauth/authorize?foo=bar"

        def inner_text(self, _sel):
            return "Authorize Codex CLI to access your account"

        @property
        def content(self):  # pragma: no cover - 不被调用
            return ""

    page = _Page()
    assert cx._recover_oauth_timeout_page(page) is False
    assert cx._recover_oauth_no_valid_organizations_page(page) is False
    assert cx._is_oauth_login_challenge_page(page) is False
