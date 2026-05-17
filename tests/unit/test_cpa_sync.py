from pathlib import Path

import pytest

from autoteam import cpa_sync


def test_list_cpa_files_raises_on_non_200(monkeypatch):
    class _Resp:
        status_code = 503
        text = "service unavailable"

        def json(self):
            raise AssertionError("json() should not be called for non-200 responses")

    monkeypatch.setattr(cpa_sync.requests, "get", lambda *_args, **_kwargs: _Resp())

    with pytest.raises(RuntimeError, match="auth-files list failed"):
        cpa_sync.list_cpa_files()


def test_list_cpa_files_raises_on_non_json(monkeypatch):
    class _Resp:
        status_code = 200
        text = "<html>not json</html>"

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(cpa_sync.requests, "get", lambda *_args, **_kwargs: _Resp())

    with pytest.raises(RuntimeError, match="returned non-JSON"):
        cpa_sync.list_cpa_files()


def test_sync_to_cpa_skips_disabled_accounts_and_keeps_protected_remote(monkeypatch, tmp_path):
    enabled_auth = tmp_path / "codex-enabled@example.com-team-a.json"
    disabled_auth = tmp_path / "codex-disabled@example.com-team-b.json"
    enabled_auth.write_text('{"access_token":"token-enabled"}', encoding="utf-8")
    disabled_auth.write_text('{"access_token":"token-disabled"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "enabled@example.com",
                "status": "active",
                "auth_file": str(enabled_auth),
                "disabled": False,
            },
            {
                "email": "disabled@example.com",
                "status": "active",
                "auth_file": str(disabled_auth),
                "disabled": True,
            },
        ],
    )
    monkeypatch.setattr("autoteam.accounts.save_accounts", lambda _accounts: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda _accounts: (0, False))
    monkeypatch.setattr(
        cpa_sync,
        "list_cpa_files",
        lambda: [
            {"name": enabled_auth.name, "email": "enabled@example.com"},
            {"name": disabled_auth.name, "email": "disabled@example.com"},
        ],
    )

    uploaded = []
    deleted = []
    monkeypatch.setattr(cpa_sync, "upload_to_cpa", lambda path: uploaded.append(Path(path).name) or True)
    monkeypatch.setattr(cpa_sync, "delete_from_cpa", lambda name: deleted.append(name) or True)

    result = cpa_sync.sync_to_cpa()

    assert uploaded == [enabled_auth.name]
    assert deleted == []
    assert result["disabled_skipped"] == 1
    assert result["delete_guard"]["skipped_protected"] == 1


def test_sync_to_cpa_refreshes_proxy_url_before_upload(monkeypatch, tmp_path):
    auth_file = tmp_path / "codex-enabled@example.com-team-a.json"
    auth_file.write_text('{"email":"enabled@example.com","access_token":"token-enabled"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "enabled@example.com",
                "status": "active",
                "auth_file": str(auth_file),
                "disabled": False,
            }
        ],
    )
    monkeypatch.setattr("autoteam.accounts.save_accounts", lambda _accounts: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda _accounts: (0, False))
    monkeypatch.setattr(cpa_sync, "list_cpa_files", lambda: [])
    monkeypatch.setattr(
        "autoteam.ipv6_pool.ipv6_pool.ensure",
        lambda _email: "socks5://proxy.example:30000",
    )

    uploaded = []
    monkeypatch.setattr(cpa_sync, "upload_to_cpa", lambda path: uploaded.append(Path(path).read_text()) or True)

    result = cpa_sync.sync_to_cpa()

    assert result["uploaded"] == 1
    assert '"proxy_url": "socks5://proxy.example:30000"' in uploaded[0]
