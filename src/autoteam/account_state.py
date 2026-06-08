"""Account state machine, transition log and event bus.

Round 12 S1 — collapses every `update_account(email, status=...)` call site into
a single transition entry-point so that:

1. Illegal transitions (e.g. ``ARCHIVED → EXHAUSTED``) raise
   :class:`IllegalTransitionError` instead of silently corrupting accounts.json.
2. Every state change is appended to ``state_log.jsonl`` atomically
   (snapshot ``.bak`` → write ``.tmp`` → ``os.replace`` → delete ``.bak``)
   so that a crash mid-write never leaves a partial JSONL line.
3. Subscribers (e.g. SSE pushers, metrics collectors) are notified through
   an in-process event bus.

The state machine is intentionally string-compatible with the legacy
``STATUS_*`` literals defined in :mod:`autoteam.accounts` — :class:`AccountState`
inherits from ``str``, so direct ``acc["status"] == AccountState.ACTIVE``
comparisons keep working without any schema migration.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Account state enum
# ---------------------------------------------------------------------------
class AccountState(str, Enum):
    """Account lifecycle states.

    The string ``value`` of each member matches the legacy ``STATUS_*``
    literals in :mod:`autoteam.accounts`, so existing accounts.json rows
    work without migration::

        acc["status"] == AccountState.ACTIVE  # True for {"status": "active"}
    """

    PENDING = "pending"               # invited, registration not yet complete
    AUTH_PENDING = "auth_invalid"     # token invalid, awaiting re-auth/repair
    ACTIVE = "active"                 # in-team, quota available
    EXHAUSTED = "exhausted"           # in-team, quota used up
    STANDBY = "standby"               # kicked out, awaiting quota recovery
    ARCHIVED = "personal"             # moved to personal Codex, off rotation
    ORPHAN = "orphan"                 # workspace seat without local auth
    DEGRADED_GRACE = "degraded_grace" # master cancel_at_period_end grace window

    @classmethod
    def coerce(cls, value: object) -> AccountState | None:
        """Cast ``value`` to :class:`AccountState`.

        - ``None`` passes through (means "no current state" — fresh row).
        - :class:`AccountState` returns as-is.
        - ``str`` is mapped via the enum value table.
        - Anything else (or unknown string) raises ``ValueError`` so
          typos are caught early instead of silently bypassing the table.
        """
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError as exc:
                raise ValueError(f"unknown account state: {value!r}") from exc
        raise TypeError(f"cannot coerce {type(value).__name__} to AccountState")


class IllegalTransitionError(ValueError):
    """Raised when a requested state transition is not in the legal table."""

    def __init__(
        self,
        email: str,
        from_state: AccountState | None,
        to_state: AccountState,
        reason: str,
    ) -> None:
        super().__init__(
            f"illegal transition for {email}: "
            f"{from_state.value if from_state else None!r} -> {to_state.value!r} "
            f"(reason={reason!r})"
        )
        self.email = email
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason


# ---------------------------------------------------------------------------
# Legal-transition table.
#
# ``None`` represents "the account does not exist yet" (initial transition
# from :func:`autoteam.accounts.add_account`). Self-loops are permitted on
# every state so that legacy callers re-asserting the current status do not
# raise — only *meaningful* changes go through the bus.
# ---------------------------------------------------------------------------
_LEGAL_TRANSITIONS: dict[AccountState | None, frozenset[AccountState]] = {
    None: frozenset({
        AccountState.PENDING,
        AccountState.ACTIVE,
        AccountState.AUTH_PENDING,
    }),
    AccountState.PENDING: frozenset({
        AccountState.PENDING,
        AccountState.ACTIVE,
        AccountState.AUTH_PENDING,
        AccountState.STANDBY,
        AccountState.ARCHIVED,
        AccountState.ORPHAN,
    }),
    AccountState.ACTIVE: frozenset({
        AccountState.ACTIVE,
        AccountState.EXHAUSTED,
        AccountState.STANDBY,
        AccountState.AUTH_PENDING,
        AccountState.ARCHIVED,
        AccountState.ORPHAN,
        AccountState.DEGRADED_GRACE,
    }),
    AccountState.EXHAUSTED: frozenset({
        AccountState.EXHAUSTED,
        AccountState.STANDBY,
        AccountState.ACTIVE,
        AccountState.AUTH_PENDING,
        AccountState.ARCHIVED,
        AccountState.DEGRADED_GRACE,
    }),
    AccountState.STANDBY: frozenset({
        AccountState.STANDBY,
        AccountState.ACTIVE,
        AccountState.AUTH_PENDING,
        AccountState.ARCHIVED,
        AccountState.PENDING,
        AccountState.EXHAUSTED,
    }),
    AccountState.AUTH_PENDING: frozenset({
        AccountState.AUTH_PENDING,
        AccountState.ACTIVE,
        AccountState.STANDBY,
        AccountState.ARCHIVED,
        AccountState.PENDING,
        AccountState.ORPHAN,
        AccountState.EXHAUSTED,
    }),
    AccountState.ARCHIVED: frozenset({
        # ARCHIVED is a "tombstone-ish" state — only re-onboarding paths exit.
        AccountState.ARCHIVED,
        AccountState.ACTIVE,
        AccountState.PENDING,
        AccountState.STANDBY,
        AccountState.AUTH_PENDING,
    }),
    AccountState.ORPHAN: frozenset({
        AccountState.ORPHAN,
        AccountState.STANDBY,
        AccountState.AUTH_PENDING,
        AccountState.ARCHIVED,
        AccountState.ACTIVE,
        AccountState.EXHAUSTED,
    }),
    AccountState.DEGRADED_GRACE: frozenset({
        AccountState.DEGRADED_GRACE,
        AccountState.STANDBY,
        AccountState.ACTIVE,
        AccountState.AUTH_PENDING,
        AccountState.EXHAUSTED,
        AccountState.ARCHIVED,
    }),
}


# ---------------------------------------------------------------------------
# Sentinel for "from_state argument not provided" — distinct from explicit
# ``None`` which means "I asserted the account does not yet exist".
# ---------------------------------------------------------------------------
_UNSET = object()


# ---------------------------------------------------------------------------
# Immutable transition record
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Transition:
    """An immutable record of a single account state transition."""

    email: str
    from_state: AccountState | None
    to_state: AccountState
    reason: str
    timestamp: float
    extra: dict[str, Any] = field(default_factory=dict)

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line (no trailing newline)."""
        payload: dict[str, Any] = {
            "email": self.email,
            "from_state": self.from_state.value if self.from_state else None,
            "to_state": self.to_state.value,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "extra": self.extra or {},
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


SubscriberCallback = Callable[[Transition], None]
StateProvider = Callable[[str], object | None]


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
class StateMachine:
    """In-process state machine, JSONL transition log and event bus."""

    def __init__(
        self,
        log_path: Path | None = None,
        state_provider: StateProvider | None = None,
    ) -> None:
        self._log_path: Path | None = Path(log_path) if log_path else None
        self._state_provider: StateProvider | None = state_provider
        # Single Lock is enough — log writes and subscriber list mutations
        # are both short, low-contention critical sections. Subscriber
        # *dispatch* happens outside the lock to avoid re-entry deadlock
        # when a callback (un)subscribes synchronously.
        self._lock = threading.Lock()
        self._subscribers: list[SubscriberCallback] = []

    # ------------------------------------------------------------------ config
    def set_state_provider(self, provider: StateProvider | None) -> None:
        """Register a callable used to look up the *current* state by email.

        Must accept one positional arg (email) and return either an
        :class:`AccountState`, the matching string literal, or ``None`` if
        the account does not yet exist. Used by :mod:`autoteam.accounts`
        to break the import cycle.
        """
        self._state_provider = provider

    @property
    def log_path(self) -> Path | None:
        return self._log_path

    # ----------------------------------------------------------- legal table
    @staticmethod
    def get_legal_transitions(
        from_state: AccountState | str | None,
    ) -> frozenset[AccountState]:
        """Return the set of states reachable from ``from_state``."""
        coerced = AccountState.coerce(from_state) if from_state is not None else None
        return _LEGAL_TRANSITIONS.get(coerced, frozenset())

    @classmethod
    def is_legal(
        cls,
        from_state: AccountState | str | None,
        to_state: AccountState | str,
    ) -> bool:
        target = AccountState.coerce(to_state)
        if target is None:
            return False
        return target in cls.get_legal_transitions(from_state)

    # -------------------------------------------------------------- event bus
    def subscribe(self, callback: SubscriberCallback) -> SubscriberCallback:
        """Register ``callback`` for future transitions. Idempotent."""
        if not callable(callback):
            raise TypeError("subscribe expects a callable")
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)
        return callback

    def unsubscribe(self, callback: SubscriberCallback) -> bool:
        """Unregister ``callback``. Returns ``True`` if it was present."""
        with self._lock:
            try:
                self._subscribers.remove(callback)
                return True
            except ValueError:
                return False

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def _publish(self, transition: Transition) -> None:
        # Snapshot under lock, dispatch outside lock to avoid re-entry deadlock.
        with self._lock:
            subscribers = list(self._subscribers)
        for cb in subscribers:
            try:
                cb(transition)
            except Exception:
                logger.exception(
                    "state_event subscriber %r raised on transition %s",
                    cb,
                    transition,
                )

    # ----------------------------------------------------------- transition
    def transition(
        self,
        email: str,
        to_state: AccountState | str,
        reason: str,
        extra: dict[str, Any] | None = None,
        *,
        from_state: Any = _UNSET,
    ) -> Transition:
        """Validate, persist and publish a state transition.

        ``from_state`` resolution order:

        1. Explicit kwarg (``None`` is honored as "fresh row" — distinct
           from leaving the kwarg unset). Used by tests / migration scripts /
           ``add_account``.
        2. ``state_provider`` callback (used by the production
           ``update_account`` shim).
        3. ``None`` — treat as "fresh row", only PENDING-class transitions
           are legal.
        """
        if not email or not isinstance(email, str):
            raise ValueError("email is required and must be a non-empty string")
        target = AccountState.coerce(to_state)
        if target is None:
            raise ValueError("to_state is required and must be a known state")

        if from_state is not _UNSET:
            prev = AccountState.coerce(from_state) if from_state is not None else None
        elif self._state_provider is not None:
            try:
                raw_prev = self._state_provider(email)
            except Exception:
                logger.exception("state_provider lookup failed for %s", email)
                raw_prev = None
            try:
                prev = AccountState.coerce(raw_prev) if raw_prev is not None else None
            except (ValueError, TypeError):
                logger.warning(
                    "state_provider returned unknown state %r for %s; treating as None",
                    raw_prev,
                    email,
                )
                prev = None
        else:
            prev = None

        legal = _LEGAL_TRANSITIONS.get(prev)
        if legal is None or target not in legal:
            raise IllegalTransitionError(email, prev, target, reason)

        record = Transition(
            email=email,
            from_state=prev,
            to_state=target,
            reason=reason or "",
            timestamp=time.time(),
            extra=dict(extra or {}),
        )

        if self._log_path is not None:
            self._write_state_log(record)
        logger.info(
            "state.transition email=%s %s->%s reason=%s",
            email,
            prev.value if prev else "<new>",
            target.value,
            reason,
        )
        self._publish(record)
        return record

    # ----------------------------------------------------------- jsonl log
    def _write_state_log(self, transition: Transition) -> None:
        """Atomically append ``transition`` as one JSONL line.

        Strategy::

            existing = read(path)
            shutil.copy2(path, path.bak)        # rollback snapshot
            write(path.tmp, existing + new_line)
            os.replace(path.tmp, path)
            unlink(path.bak)

        On any OSError after the snapshot succeeds, the ``.bak`` is
        restored back over ``path`` and the exception re-raised.
        """
        path = self._log_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        bak_path = path.with_suffix(path.suffix + ".bak")
        tmp_path = path.with_suffix(path.suffix + ".tmp")

        new_line = (transition.to_jsonl() + "\n").encode("utf-8")

        with self._lock:  # serialize log writes across threads
            existing = b""
            had_snapshot = False
            if path.exists():
                try:
                    existing = path.read_bytes()
                except OSError:
                    logger.exception("state_log: cannot read existing %s", path)
                    raise
                try:
                    shutil.copy2(path, bak_path)
                    had_snapshot = True
                except OSError as exc:
                    # snapshot is best-effort; without it we lose rollback
                    # but still attempt the write so the new transition lands.
                    logger.warning(
                        "state_log: cannot snapshot %s -> %s: %s",
                        path,
                        bak_path,
                        exc,
                    )

            try:
                with open(tmp_path, "wb") as fp:
                    if existing:
                        fp.write(existing)
                        if not existing.endswith(b"\n"):
                            fp.write(b"\n")
                    fp.write(new_line)
                    fp.flush()
                    try:
                        os.fsync(fp.fileno())
                    except OSError:
                        # fsync is best-effort on some FS / Windows shares
                        logger.debug("state_log: fsync failed (non-fatal)")
                os.replace(tmp_path, path)
            except OSError:
                logger.exception(
                    "state_log: write failed for %s, attempting rollback", path
                )
                if had_snapshot and bak_path.exists():
                    try:
                        os.replace(bak_path, path)
                    except OSError:
                        logger.exception(
                            "state_log: rollback failed for %s", path
                        )
                # cleanup orphan tmp
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                raise
            else:
                if had_snapshot and bak_path.exists():
                    try:
                        bak_path.unlink()
                    except OSError:
                        logger.warning(
                            "state_log: cannot delete .bak %s", bak_path
                        )


# ---------------------------------------------------------------------------
# Module-level default machine.
#
# Tests instantiate their own :class:`StateMachine` per fixture; production
# code (``autoteam.accounts``) uses ``default_machine`` so subscribers wired
# in one place see every transition.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_LOG_PATH = _PROJECT_ROOT / "state_log.jsonl"

default_machine = StateMachine(log_path=DEFAULT_LOG_PATH)


# ---------------------------------------------------------------------------
# One-shot migration helper
# ---------------------------------------------------------------------------
def migrate_legacy_status(
    accounts_iter: Iterable[dict[str, Any]],
) -> tuple[int, list[str]]:
    """Normalize ``status`` field on a sequence of account dicts.

    Mutates each dict in place:

    - Missing / ``None`` ``status`` → ``AccountState.PENDING.value``.
    - Unknown literal → left untouched, recorded in the returned list so
      callers (e.g. a one-shot migration script) can decide what to do.

    Returns ``(migrated_count, unknown_states)``. The caller is expected
    to write a ``.bak`` of the source file *before* invoking this helper.
    """
    migrated = 0
    unknown: list[str] = []
    for acc in accounts_iter:
        raw = acc.get("status")
        if raw is None or raw == "":
            acc["status"] = AccountState.PENDING.value
            migrated += 1
            continue
        try:
            AccountState(raw)
        except ValueError:
            unknown.append(str(raw))
    return migrated, unknown


__all__ = [
    "AccountState",
    "DEFAULT_LOG_PATH",
    "IllegalTransitionError",
    "StateMachine",
    "Transition",
    "default_machine",
    "migrate_legacy_status",
]
