from autoteam import manager


class _FakeStartedChatGPT:
    browser = True

    def __init__(self, members):
        self.members = list(members)

    def _api_fetch(self, method, path):
        assert method == "GET"
        assert path.endswith("/users")
        import json

        return {"status": 200, "body": json.dumps({"items": self.members})}

    def stop(self):
        self.browser = False


def test_resolve_auth_file_path_handles_container_and_project_relative_paths(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    container_auth = project_root / "data" / "auths" / "codex-manual@example.com.json"
    container_auth.parent.mkdir(parents=True)
    container_auth.write_text("{}", encoding="utf-8")
    project_auth = project_root / "auths" / "codex-team@example.com-team.json"
    project_auth.parent.mkdir(parents=True, exist_ok=True)
    project_auth.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(manager, "__file__", str(project_root / "src" / "autoteam" / "manager.py"))

    assert manager._resolve_auth_file_path("/app/data/auths/codex-manual@example.com.json") == container_auth
    assert manager._resolve_auth_file_path("data/auths/codex-manual@example.com.json") == container_auth
    assert manager._resolve_auth_file_path("auths/codex-team@example.com-team.json") == project_auth
    assert manager._has_auth_file({"auth_file": "/app/data/auths/codex-manual@example.com.json"}) is True


def test_find_team_auth_file_uses_search_dirs(tmp_path, monkeypatch):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    auth_file = auth_dir / "codex-user@example.com-team-1234.json"
    auth_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(manager, "_auth_search_dirs", lambda: (auth_dir,))

    assert manager._find_team_auth_file("user@example.com") == str(auth_file)


def test_sync_account_states_recovers_team_auth_file_as_protected(tmp_path, monkeypatch):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    auth_file = auth_dir / "codex-manual@example.com-team.json"
    auth_file.write_text('{"email": "manual@example.com"}', encoding="utf-8")
    saved = []

    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acc-1")
    monkeypatch.setattr(manager, "load_accounts", lambda: [])
    monkeypatch.setattr(manager, "save_accounts", lambda accounts: saved.extend(accounts))
    monkeypatch.setattr("autoteam.codex_auth.AUTH_DIR", auth_dir)

    chatgpt = _FakeStartedChatGPT([{"email": "manual@example.com", "user_id": "u-1"}])
    manager.sync_account_states(chatgpt_api=chatgpt)

    assert len(saved) == 1
    assert saved[0]["email"] == "manual@example.com"
    assert saved[0]["status"] == "active"
    assert saved[0]["auth_file"] == str(auth_file)
    assert saved[0]["workspace_account_id"] == "acc-1"
    assert saved[0]["protect_team_seat"] is True


def test_sync_account_states_protects_existing_standby_auth_file(tmp_path, monkeypatch):
    auth_file = tmp_path / "codex-member@example.com-team.json"
    auth_file.write_text("{}", encoding="utf-8")
    empty_auth_dir = tmp_path / "empty-auths"
    empty_auth_dir.mkdir()
    stored = [
        {
            "email": "member@example.com",
            "status": "standby",
            "auth_file": str(auth_file),
            "disabled": False,
        }
    ]
    saved = []

    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acc-1")
    monkeypatch.setattr(manager, "load_accounts", lambda: [dict(item) for item in stored])
    monkeypatch.setattr(manager, "save_accounts", lambda accounts: saved.extend(accounts))
    monkeypatch.setattr(manager, "update_account", lambda _email, **_fields: None)
    monkeypatch.setattr("autoteam.codex_auth.AUTH_DIR", empty_auth_dir)

    chatgpt = _FakeStartedChatGPT([{"email": "member@example.com", "user_id": "u-1"}])
    manager.sync_account_states(chatgpt_api=chatgpt)

    assert len(saved) == 1
    assert saved[0]["status"] == "active"
    assert saved[0]["workspace_account_id"] == "acc-1"
    assert saved[0]["protect_team_seat"] is True
