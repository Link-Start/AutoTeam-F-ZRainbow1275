"""Round 12 S1 — Account state machine, transition log, event bus."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from autoteam import accounts as accounts_mod
from autoteam.account_state import (
    AccountState,
    IllegalTransitionError,
    StateMachine,
    Transition,
    default_machine,
    migrate_legacy_status,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_machine(tmp_path: Path) -> StateMachine:
    """Fresh StateMachine pointing at an isolated tmp log file."""
    return StateMachine(log_path=tmp_path / "state_log.jsonl")


@pytest.fixture
def isolated_accounts(tmp_path: Path, monkeypatch):
    """Isolate accounts.json + default_machine log to tmp_path."""
    accounts_file = tmp_path / "accounts.json"
    log_file = tmp_path / "state_log.jsonl"
    monkeypatch.setattr(accounts_mod, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts_mod, "get_admin_email", lambda: "")
    # redirect default_machine's log path; restore on teardown
    original_log = default_machine._log_path
    default_machine._log_path = log_file
    yield {"accounts_file": accounts_file, "log_file": log_file}
    default_machine._log_path = original_log


# ---------------------------------------------------------------------------
# Enum coercion
# ---------------------------------------------------------------------------
class TestAccountStateEnum:
    def test_value_aligns_with_legacy_string_literals(self):
        assert AccountState.PENDING.value == "pending"
        assert AccountState.ACTIVE.value == "active"
        assert AccountState.EXHAUSTED.value == "exhausted"
        assert AccountState.STANDBY.value == "standby"
        assert AccountState.AUTH_PENDING.value == "auth_invalid"
        assert AccountState.ARCHIVED.value == "personal"
        assert AccountState.ORPHAN.value == "orphan"
        assert AccountState.DEGRADED_GRACE.value == "degraded_grace"

    def test_string_compatibility_with_legacy_status(self):
        # 现有代码 acc["status"] == "active" 必须仍然 True
        assert AccountState.ACTIVE == "active"

    def test_coerce_passes_none(self):
        assert AccountState.coerce(None) is None

    def test_coerce_passes_enum(self):
        assert AccountState.coerce(AccountState.PENDING) is AccountState.PENDING

    def test_coerce_string_value(self):
        assert AccountState.coerce("active") is AccountState.ACTIVE

    def test_coerce_unknown_string_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown account state"):
            AccountState.coerce("not_a_state")

    def test_coerce_invalid_type_raises_type_error(self):
        with pytest.raises(TypeError):
            AccountState.coerce(123)


# ---------------------------------------------------------------------------
# Legal transitions
# ---------------------------------------------------------------------------
class TestLegalTransitions:
    @pytest.mark.parametrize("from_state,to_state", [
        (None, AccountState.PENDING),
        (AccountState.PENDING, AccountState.ACTIVE),
        (AccountState.ACTIVE, AccountState.EXHAUSTED),
        (AccountState.EXHAUSTED, AccountState.STANDBY),
        (AccountState.STANDBY, AccountState.ACTIVE),
        (AccountState.ACTIVE, AccountState.AUTH_PENDING),
        (AccountState.AUTH_PENDING, AccountState.ACTIVE),
        (AccountState.ACTIVE, AccountState.ARCHIVED),
        (AccountState.ARCHIVED, AccountState.ACTIVE),
        (AccountState.ACTIVE, AccountState.DEGRADED_GRACE),
        (AccountState.DEGRADED_GRACE, AccountState.STANDBY),
        (AccountState.ACTIVE, AccountState.ORPHAN),
        (AccountState.ORPHAN, AccountState.STANDBY),
    ])
    def test_legal_path(self, from_state, to_state):
        assert StateMachine.is_legal(from_state, to_state)

    @pytest.mark.parametrize("from_state,to_state", [
        # cannot bypass PENDING for first registration into in-team states
        (None, AccountState.STANDBY),
        (None, AccountState.EXHAUSTED),
        (None, AccountState.ARCHIVED),
        (None, AccountState.ORPHAN),
        (None, AccountState.DEGRADED_GRACE),
        # ARCHIVED is tombstone-ish: cannot drop straight into in-team
        # exhaustion / orphan / grace states without re-onboarding
        (AccountState.ARCHIVED, AccountState.EXHAUSTED),
        (AccountState.ARCHIVED, AccountState.ORPHAN),
        (AccountState.ARCHIVED, AccountState.DEGRADED_GRACE),
        # PENDING is pre-team; cannot fast-forward to mid-cycle states
        (AccountState.PENDING, AccountState.EXHAUSTED),
        (AccountState.PENDING, AccountState.DEGRADED_GRACE),
    ])
    def test_illegal_path(self, from_state, to_state):
        assert not StateMachine.is_legal(from_state, to_state)

    def test_self_loop_allowed_for_idempotent_callers(self):
        # legacy callers might re-assert current status — must NOT raise
        for state in AccountState:
            assert StateMachine.is_legal(state, state), state

    def test_get_legal_transitions_returns_frozenset(self):
        result = StateMachine.get_legal_transitions(AccountState.ACTIVE)
        assert isinstance(result, frozenset)
        assert AccountState.EXHAUSTED in result

    def test_get_legal_transitions_accepts_string(self):
        result = StateMachine.get_legal_transitions("active")
        assert AccountState.STANDBY in result


# ---------------------------------------------------------------------------
# transition() — happy + error paths
# ---------------------------------------------------------------------------
class TestTransitionExecution:
    def test_happy_path_returns_transition_record(self, isolated_machine):
        t = isolated_machine.transition(
            email="user@example.com",
            to_state=AccountState.PENDING,
            reason="add_account",
            from_state=None,
        )
        assert isinstance(t, Transition)
        assert t.email == "user@example.com"
        assert t.from_state is None
        assert t.to_state is AccountState.PENDING
        assert t.reason == "add_account"
        assert t.timestamp > 0

    def test_string_to_state_is_coerced(self, isolated_machine):
        t = isolated_machine.transition(
            email="x@example.com",
            to_state="active",
            reason="ok",
            from_state="pending",
        )
        assert t.to_state is AccountState.ACTIVE
        assert t.from_state is AccountState.PENDING

    def test_extra_payload_persisted(self, isolated_machine):
        t = isolated_machine.transition(
            email="x@example.com",
            to_state=AccountState.ACTIVE,
            reason="register",
            extra={"workspace_account_id": "ws-123"},
            from_state=AccountState.PENDING,
        )
        assert t.extra == {"workspace_account_id": "ws-123"}

    def test_illegal_transition_raises(self, isolated_machine):
        with pytest.raises(IllegalTransitionError) as excinfo:
            isolated_machine.transition(
                email="x@example.com",
                to_state=AccountState.EXHAUSTED,
                reason="bug",
                from_state=AccountState.ARCHIVED,
            )
        err = excinfo.value
        assert err.email == "x@example.com"
        assert err.from_state is AccountState.ARCHIVED
        assert err.to_state is AccountState.EXHAUSTED
        assert err.reason == "bug"

    def test_illegal_initial_transition(self, isolated_machine):
        with pytest.raises(IllegalTransitionError):
            isolated_machine.transition(
                email="x@example.com",
                to_state=AccountState.STANDBY,
                reason="cannot create directly into standby",
                from_state=None,
            )

    def test_unknown_to_state_raises_value_error(self, isolated_machine):
        with pytest.raises(ValueError, match="unknown account state"):
            isolated_machine.transition(
                email="x@example.com",
                to_state="bogus_state",
                reason="typo",
                from_state=None,
            )

    def test_empty_email_raises(self, isolated_machine):
        with pytest.raises(ValueError, match="email is required"):
            isolated_machine.transition(
                email="",
                to_state=AccountState.PENDING,
                reason="r",
                from_state=None,
            )

    def test_state_provider_drives_from_state(self, tmp_path):
        machine = StateMachine(
            log_path=tmp_path / "state_log.jsonl",
            state_provider=lambda email: "active" if email == "u@x.com" else None,
        )
        t = machine.transition(
            email="u@x.com",
            to_state=AccountState.EXHAUSTED,
            reason="quota",
        )
        assert t.from_state is AccountState.ACTIVE

    def test_state_provider_unknown_string_falls_back_to_none(self, tmp_path):
        machine = StateMachine(
            log_path=tmp_path / "state_log.jsonl",
            state_provider=lambda email: "garbage_state",
        )
        # provider returned unknown → treated as None → only PENDING/ACTIVE/AUTH_PENDING legal
        t = machine.transition(
            email="u@x.com",
            to_state=AccountState.PENDING,
            reason="recover",
        )
        assert t.from_state is None

    def test_state_provider_exception_does_not_break_transition(self, tmp_path):
        def boom(_email):
            raise RuntimeError("db down")

        machine = StateMachine(
            log_path=tmp_path / "state_log.jsonl",
            state_provider=boom,
        )
        t = machine.transition(
            email="u@x.com",
            to_state=AccountState.PENDING,
            reason="resilient",
        )
        assert t.from_state is None


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------
class TestEventBus:
    def test_subscriber_receives_transition(self, isolated_machine):
        received: list[Transition] = []
        isolated_machine.subscribe(received.append)
        isolated_machine.transition(
            email="a@x.com",
            to_state=AccountState.PENDING,
            reason="r",
            from_state=None,
        )
        assert len(received) == 1
        assert received[0].email == "a@x.com"

    def test_subscribe_is_idempotent(self, isolated_machine):
        cb = lambda _t: None  # noqa: E731
        isolated_machine.subscribe(cb)
        isolated_machine.subscribe(cb)
        assert isolated_machine.subscriber_count() == 1

    def test_unsubscribe_removes_callback(self, isolated_machine):
        received: list[Transition] = []
        cb = received.append
        isolated_machine.subscribe(cb)
        assert isolated_machine.unsubscribe(cb) is True
        # second unsubscribe should report False (already gone)
        assert isolated_machine.unsubscribe(cb) is False
        isolated_machine.transition(
            email="a@x.com",
            to_state=AccountState.PENDING,
            reason="r",
            from_state=None,
        )
        assert received == []

    def test_multiple_subscribers_all_invoked(self, isolated_machine):
        received_a: list[Transition] = []
        received_b: list[Transition] = []
        isolated_machine.subscribe(received_a.append)
        isolated_machine.subscribe(received_b.append)
        isolated_machine.transition(
            email="a@x.com",
            to_state=AccountState.PENDING,
            reason="r",
            from_state=None,
        )
        assert len(received_a) == 1
        assert len(received_b) == 1

    def test_subscriber_exception_does_not_block_others(self, isolated_machine, caplog):
        good_received: list[Transition] = []

        def bad(_t):
            raise RuntimeError("subscriber crashed")

        isolated_machine.subscribe(bad)
        isolated_machine.subscribe(good_received.append)
        with caplog.at_level("ERROR"):
            isolated_machine.transition(
                email="a@x.com",
                to_state=AccountState.PENDING,
                reason="r",
                from_state=None,
            )
        assert len(good_received) == 1
        assert any("state_event subscriber" in rec.message for rec in caplog.records)

    def test_subscribe_non_callable_raises(self, isolated_machine):
        with pytest.raises(TypeError):
            isolated_machine.subscribe("not callable")  # type: ignore[arg-type]

    def test_concurrent_subscribers_threadsafe(self, isolated_machine):
        """N threads each subscribe + unsubscribe + emit: no exception, count consistent."""
        N = 20
        barrier = threading.Barrier(N)
        errors: list[BaseException] = []

        def worker(idx: int):
            try:
                barrier.wait()
                cb = lambda _t, _i=idx: None  # noqa: E731
                isolated_machine.subscribe(cb)
                isolated_machine.transition(
                    email=f"u{idx}@x.com",
                    to_state=AccountState.PENDING,
                    reason=f"thread_{idx}",
                    from_state=None,
                )
                isolated_machine.unsubscribe(cb)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert isolated_machine.subscriber_count() == 0


# ---------------------------------------------------------------------------
# JSONL state log
# ---------------------------------------------------------------------------
class TestStateLog:
    def test_first_write_creates_file(self, isolated_machine, tmp_path):
        log_path = isolated_machine.log_path
        assert log_path is not None
        assert not log_path.exists()
        isolated_machine.transition(
            email="a@x.com",
            to_state=AccountState.PENDING,
            reason="add_account",
            from_state=None,
        )
        assert log_path.exists()

    def test_jsonl_format(self, isolated_machine):
        isolated_machine.transition(
            email="a@x.com",
            to_state=AccountState.PENDING,
            reason="add_account",
            from_state=None,
            extra={"k": "v"},
        )
        log_path = isolated_machine.log_path
        content = log_path.read_text(encoding="utf-8").splitlines()
        assert len(content) == 1
        record = json.loads(content[0])
        assert record["email"] == "a@x.com"
        assert record["from_state"] is None
        assert record["to_state"] == "pending"
        assert record["reason"] == "add_account"
        assert record["extra"] == {"k": "v"}
        assert isinstance(record["timestamp"], (int, float))

    def test_appends_preserve_ordering(self, isolated_machine):
        for n, target in [
            (None, AccountState.PENDING),
            (AccountState.PENDING, AccountState.ACTIVE),
            (AccountState.ACTIVE, AccountState.EXHAUSTED),
        ]:
            isolated_machine.transition(
                email="a@x.com",
                to_state=target,
                reason=f"step-{target.value}",
                from_state=n,
            )
        lines = isolated_machine.log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        targets = [json.loads(line)["to_state"] for line in lines]
        assert targets == ["pending", "active", "exhausted"]

    def test_atomic_rollback_on_replace_failure(
        self, isolated_machine, monkeypatch
    ):
        """If os.replace raises mid-write, the existing log must be restored from .bak."""
        log_path = isolated_machine.log_path
        # seed with one transition first
        isolated_machine.transition(
            email="a@x.com",
            to_state=AccountState.PENDING,
            reason="first",
            from_state=None,
        )
        original_bytes = log_path.read_bytes()

        # now sabotage os.replace inside _write_state_log path
        real_replace = os.replace

        def flaky_replace(src, dst, *args, **kwargs):
            src_path = Path(str(src))
            # only fail when we're trying to replace the .tmp -> log file
            if src_path.suffix == ".tmp":
                raise OSError("simulated replace failure")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr("autoteam.account_state.os.replace", flaky_replace)

        with pytest.raises(OSError, match="simulated replace failure"):
            isolated_machine.transition(
                email="a@x.com",
                to_state=AccountState.ACTIVE,
                reason="second",
                from_state=AccountState.PENDING,
            )

        # state_log.jsonl content unchanged: rollback succeeded
        assert log_path.read_bytes() == original_bytes
        # tmp / bak cleaned up
        assert not log_path.with_suffix(".jsonl.tmp").exists()
        # bak removed by rollback path (it's now back to being log_path)
        assert not log_path.with_suffix(".jsonl.bak").exists()

    def test_no_log_path_skips_disk_write(self, tmp_path):
        machine = StateMachine(log_path=None)
        # should not raise even though there's no file
        t = machine.transition(
            email="a@x.com",
            to_state=AccountState.PENDING,
            reason="memory_only",
            from_state=None,
        )
        assert t.email == "a@x.com"


# ---------------------------------------------------------------------------
# Migration helper
# ---------------------------------------------------------------------------
class TestMigrateLegacyStatus:
    def test_none_status_normalized_to_pending(self):
        accounts = [{"email": "a"}, {"email": "b", "status": None}]
        migrated, unknown = migrate_legacy_status(accounts)
        assert migrated == 2
        assert unknown == []
        assert accounts[0]["status"] == "pending"
        assert accounts[1]["status"] == "pending"

    def test_known_status_left_untouched(self):
        accounts = [{"email": "a", "status": "active"}]
        migrated, unknown = migrate_legacy_status(accounts)
        assert migrated == 0
        assert unknown == []
        assert accounts[0]["status"] == "active"

    def test_unknown_status_recorded(self):
        accounts = [{"email": "a", "status": "ghost"}]
        migrated, unknown = migrate_legacy_status(accounts)
        assert migrated == 0
        assert unknown == ["ghost"]
        # left untouched so a human can decide
        assert accounts[0]["status"] == "ghost"


# ---------------------------------------------------------------------------
# Integration with autoteam.accounts
# ---------------------------------------------------------------------------
class TestAccountsIntegration:
    def test_add_account_emits_pending_transition(self, isolated_accounts):
        received: list[Transition] = []
        default_machine.subscribe(received.append)
        try:
            accounts_mod.add_account("u@example.com", "secret")
        finally:
            default_machine.unsubscribe(received.append)

        # state file persisted PENDING
        rows = accounts_mod.load_accounts()
        assert rows[0]["status"] == "pending"
        # event emitted None -> PENDING
        assert len(received) == 1
        assert received[0].from_state is None
        assert received[0].to_state is AccountState.PENDING
        # log line written
        assert isolated_accounts["log_file"].exists()

    def test_update_account_status_change_emits_transition(self, isolated_accounts):
        accounts_mod.add_account("u@example.com", "secret")
        received: list[Transition] = []
        default_machine.subscribe(received.append)
        try:
            accounts_mod.update_account("u@example.com", status=accounts_mod.STATUS_ACTIVE)
        finally:
            default_machine.unsubscribe(received.append)
        assert len(received) == 1
        assert received[0].from_state is AccountState.PENDING
        assert received[0].to_state is AccountState.ACTIVE
        assert accounts_mod.load_accounts()[0]["status"] == "active"

    def test_update_account_no_status_change_does_not_emit(self, isolated_accounts):
        accounts_mod.add_account("u@example.com", "secret")
        received: list[Transition] = []
        default_machine.subscribe(received.append)
        try:
            # only auth_file change, status untouched
            accounts_mod.update_account("u@example.com", auth_file="auth.json")
        finally:
            default_machine.unsubscribe(received.append)
        assert received == []
        assert accounts_mod.load_accounts()[0]["auth_file"] == "auth.json"

    def test_update_account_same_status_does_not_emit(self, isolated_accounts):
        accounts_mod.add_account("u@example.com", "secret")
        received: list[Transition] = []
        default_machine.subscribe(received.append)
        try:
            accounts_mod.update_account(
                "u@example.com", status=accounts_mod.STATUS_PENDING
            )
        finally:
            default_machine.unsubscribe(received.append)
        assert received == []

    def test_update_account_illegal_transition_raises(self, isolated_accounts):
        accounts_mod.add_account("u@example.com", "secret")
        # PENDING -> EXHAUSTED is illegal
        with pytest.raises(IllegalTransitionError):
            accounts_mod.update_account(
                "u@example.com", status=accounts_mod.STATUS_EXHAUSTED
            )
        # accounts.json must NOT be mutated when transition illegal
        assert accounts_mod.load_accounts()[0]["status"] == "pending"

    def test_update_account_unknown_email_returns_none(self, isolated_accounts):
        result = accounts_mod.update_account(
            "ghost@example.com", status=accounts_mod.STATUS_ACTIVE
        )
        assert result is None
