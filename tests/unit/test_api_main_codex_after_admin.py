import threading

import pytest
from fastapi import HTTPException

from autoteam import api


@pytest.fixture(autouse=True)
def _reset_main_codex_state():
    yield
    api._admin_login_api = None
    api._admin_login_step = None
    api._main_codex_flow = None
    api._main_codex_step = None
    api._main_codex_action = None
    if api._playwright_lock.locked():
        api._playwright_lock.release()


def test_finish_admin_login_refreshes_local_main_auth_without_remote_sync(monkeypatch):
    events = []

    class FakeAdminLoginAPI:
        def complete_admin_login(self):
            events.append("admin_complete")
            return {
                "email": "owner@example.com",
                "session_token": "session-token",
                "account_id": "acc-1",
                "workspace_name": "Idapro",
            }

        def stop(self):
            events.append("admin_stop")

    lock = threading.Lock()
    assert lock.acquire(blocking=False) is True

    def fake_refresh_main_auth_file():
        events.append("refresh_main_auth")
        return {"email": "owner@example.com", "auth_file": "/tmp/codex-main-acc-1.json", "plan_type": "team"}

    def fail_remote_sync(_filepath):
        raise AssertionError("admin login must not sync main Codex auth to remote targets")

    monkeypatch.setattr(api, "_playwright_lock", lock)
    monkeypatch.setattr(api, "_admin_login_api", FakeAdminLoginAPI())
    monkeypatch.setattr(api, "_admin_login_step", "workspace_required")
    monkeypatch.setattr(api, "_main_codex_flow", None)
    monkeypatch.setattr(api, "_main_codex_step", None)
    monkeypatch.setattr(api, "_main_codex_action", None)
    monkeypatch.setattr(api._pw_executor, "run", lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr("autoteam.codex_auth.refresh_main_auth_file", fake_refresh_main_auth_file)
    monkeypatch.setattr("autoteam.sync_targets.sync_main_codex_to_configured_targets", fail_remote_sync)
    monkeypatch.setattr(
        "autoteam.admin_state.get_admin_state_summary",
        lambda: {
            "configured": False,
            "email": "",
            "password_saved": False,
            "session_present": False,
            "account_id": "",
            "workspace_name": "",
        },
    )

    result = api._finish_admin_login({"step": "completed"})

    assert result["status"] == "completed"
    assert result["codex"] == {"in_progress": False, "step": None, "action": None}
    assert result["info"]["main_auth"] == {
        "email": "owner@example.com",
        "auth_file": "/tmp/codex-main-acc-1.json",
        "plan_type": "team",
    }
    assert api._admin_login_api is None
    assert api._admin_login_step is None
    assert lock.locked() is False
    assert events == ["admin_complete", "admin_stop", "refresh_main_auth"]


def test_finish_main_codex_flow_returns_login_message(monkeypatch):
    events = []

    class FakeMainCodexFlow:
        def complete(self):
            events.append("complete")
            return {
                "email": "owner@example.com",
                "auth_file": "/tmp/codex-main-acc-1.json",
                "plan_type": "team",
            }

        def stop(self):
            events.append("stop")

    lock = threading.Lock()
    assert lock.acquire(blocking=False) is True

    monkeypatch.setattr(api, "_playwright_lock", lock)
    monkeypatch.setattr(api, "_main_codex_flow", FakeMainCodexFlow())
    monkeypatch.setattr(api, "_main_codex_step", "code_required")
    monkeypatch.setattr(api, "_main_codex_action", "login")
    monkeypatch.setattr(api._pw_executor, "run", lambda func, *args, **kwargs: func(*args, **kwargs))

    result = api._finish_main_codex_flow()

    assert result == {
        "status": "completed",
        "message": "主号 Codex 已登录",
        "codex": {"in_progress": False, "step": None, "action": None},
        "info": {
            "email": "owner@example.com",
            "auth_file": "/tmp/codex-main-acc-1.json",
            "plan_type": "team",
        },
    }
    assert api._main_codex_flow is None
    assert api._main_codex_step is None
    assert api._main_codex_action is None
    assert lock.locked() is False
    assert events == ["complete", "stop"]


def test_post_main_codex_login_starts_login_flow(monkeypatch):
    events = []
    lock = threading.Lock()

    monkeypatch.setattr(api, "_playwright_lock", lock)
    monkeypatch.setattr(api, "_main_codex_flow", None)
    monkeypatch.setattr(api, "_main_codex_step", None)
    monkeypatch.setattr(api, "_main_codex_action", None)

    def fake_start(action="sync"):
        events.append(action)
        if lock.locked():
            lock.release()
        return "completed", {
            "status": "completed",
            "message": "主号 Codex 已登录",
            "codex": {"in_progress": False, "step": None, "action": None},
            "info": {"auth_file": "/tmp/codex-main-acc-1.json"},
        }

    monkeypatch.setattr(api, "_start_main_codex_flow", fake_start)

    result = api.post_main_codex_login()

    assert events == ["login"]
    assert result["message"] == "主号 Codex 已登录"
    assert lock.locked() is False


def test_main_codex_sync_requires_enabled_target(monkeypatch):
    monkeypatch.setenv("SYNC_TARGET_CPA", "false")
    monkeypatch.setenv("SYNC_TARGET_SUB2API", "false")
    monkeypatch.delenv("CPA_URL", raising=False)
    monkeypatch.delenv("CPA_KEY", raising=False)
    monkeypatch.delenv("SUB2API_URL", raising=False)
    monkeypatch.delenv("SUB2API_EMAIL", raising=False)
    monkeypatch.delenv("SUB2API_PASSWORD", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        api._require_sync_target_configs("同步主号 Codex")

    assert exc_info.value.status_code == 400
    assert "启用至少一个远端同步目标" in exc_info.value.detail


def test_post_main_codex_delete_remote_files_returns_deleted_names(monkeypatch):
    monkeypatch.setattr(api, "_require_sync_target_configs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "autoteam.sync_targets.get_enabled_sync_targets",
        lambda env=None: ["cpa", "sub2api"],
    )
    monkeypatch.setattr(
        "autoteam.sync_targets.delete_main_codex_from_configured_targets",
        lambda: {
            "cpa": {"deleted": ["codex-main-acc-1.json"], "count": 1},
            "sub2api": {"deleted": ["sub2api-codex-main-acc-1.json"], "count": 1},
        },
    )

    result = api.post_main_codex_delete_remote_files()

    assert result == {
        "message": "已从 CPA + Sub2API 删除 2 个主号认证文件",
        "deleted": ["codex-main-acc-1.json", "sub2api-codex-main-acc-1.json"],
        "results": {
            "cpa": {"deleted": ["codex-main-acc-1.json"], "count": 1},
            "sub2api": {"deleted": ["sub2api-codex-main-acc-1.json"], "count": 1},
        },
    }


def test_post_main_codex_delete_cpa_requires_enabled_target(monkeypatch):
    def fake_require(*_args, **_kwargs):
        raise HTTPException(
            status_code=400,
            detail="删除主号 Codex 远端文件 前请先在配置面板启用至少一个远端同步目标（CPA 或 Sub2API）",
        )

    monkeypatch.setattr(api, "_require_sync_target_configs", fake_require)

    with pytest.raises(HTTPException) as exc_info:
        api.post_main_codex_delete_cpa()

    assert exc_info.value.status_code == 400
    assert "启用至少一个远端同步目标" in exc_info.value.detail
