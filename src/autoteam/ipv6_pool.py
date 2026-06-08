"""Persistent per-account IPv6 SOCKS5 proxy pool."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import socket
import threading
import time
from pathlib import Path

from autoteam import config as _config
from autoteam.ipv6_proxy import _add_ipv6, _del_ipv6, _handle_socks5, ipv6_proxy_enabled, random_ipv6

logger = logging.getLogger(__name__)


def _normalized_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _format_proxy_host(host: str) -> str:
    host = (host or "").strip()
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _pool_file() -> Path:
    raw = getattr(_config, "IPV6_PROXY_POOL_FILE", "") or ""
    return Path(raw) if raw else Path(_config.PROJECT_ROOT) / "ipv6_pool.json"


def _listen_host() -> str:
    return getattr(_config, "IPV6_PROXY_LISTEN_HOST", "0.0.0.0") or "0.0.0.0"


def _local_host() -> str:
    return getattr(_config, "IPV6_PROXY_LOCAL_HOST", "127.0.0.1") or "127.0.0.1"


def _public_host() -> str:
    return (
        getattr(_config, "IPV6_PROXY_PUBLIC_HOST", "")
        or getattr(_config, "PUBLIC_IPV4", "")
        or _local_host()
    )


def _allowed_ips() -> set[str]:
    raw = getattr(_config, "IPV6_PROXY_ALLOWED_IPS", "") or ""
    values = {item.strip() for item in raw.split(",") if item.strip()}
    values.add("127.0.0.1")
    return values


def _max_ttl_seconds() -> int:
    return max(0, int(getattr(_config, "IPV6_PROXY_TTL_SECONDS", 2 * 24 * 3600) or 0))


class _ProxyEntry:
    def __init__(self, email: str, ipv6_addr: str, port: int, created_at: float | None = None):
        self.email = _normalized_email(email)
        self.ipv6_addr = ipv6_addr
        self.port = int(port)
        self.created_at = float(created_at or time.time())
        self._server: asyncio.base_events.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._main_task: asyncio.Task | None = None
        self._handler_tasks: set[asyncio.Task] = set()
        self._ready = threading.Event()
        self._error: BaseException | None = None

    @property
    def proxy_url(self) -> str:
        return f"socks5://{_format_proxy_host(_public_host())}:{self.port}"

    @property
    def local_proxy_url(self) -> str:
        return f"socks5://{_format_proxy_host(_local_host())}:{self.port}"

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "ipv6_addr": self.ipv6_addr,
            "port": self.port,
            "created_at": self.created_at,
        }

    def is_healthy(self) -> bool:
        if not self._server or not self._loop or not self._thread:
            return False
        if not self._loop.is_running() or not self._thread.is_alive():
            return False
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                return True
        except OSError:
            return False

    def start(self) -> None:
        if self.is_healthy():
            return
        if self._server or self._loop or self._thread:
            self.stop()

        self._ready.clear()
        self._error = None
        _add_ipv6(self.ipv6_addr)

        self._loop = asyncio.new_event_loop()
        source_addr = self.ipv6_addr
        listen_port = self.port
        ready = self._ready

        async def _checked_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            if task is not None:
                self._handler_tasks.add(task)
            try:
                peer = writer.get_extra_info("peername")
                allowed = _allowed_ips()
                if peer and "*" not in allowed and peer[0] not in allowed:
                    logger.warning("[IPv6Pool] rejected client %s for %s:%d", peer[0], self.email, listen_port)
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
                    return
                await _handle_socks5(reader, writer, source_addr)
            finally:
                if task is not None:
                    self._handler_tasks.discard(task)

        async def _run() -> None:
            server = await asyncio.start_server(_checked_handler, _listen_host(), listen_port)
            self._server = server
            ready.set()
            try:
                async with server:
                    await server.serve_forever()
            except asyncio.CancelledError:
                pass

        def _thread_main() -> None:
            assert self._loop is not None
            asyncio.set_event_loop(self._loop)
            try:
                self._main_task = self._loop.create_task(_run())
                self._loop.run_until_complete(self._main_task)
            except BaseException as exc:
                self._error = exc
                ready.set()
            finally:
                try:
                    pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                except Exception:
                    pass
                finally:
                    self._loop.close()

        self._thread = threading.Thread(target=_thread_main, daemon=True, name=f"ipv6pool-{listen_port}")
        self._thread.start()
        self._ready.wait(timeout=10)
        if self._error:
            self._clear_runtime_refs()
            raise self._error
        if not self._server:
            self._clear_runtime_refs()
            raise RuntimeError(f"IPv6Pool proxy port {self.port} startup timed out")

        logger.info("[IPv6Pool] started %s -> %s (exit %s)", self.email, self.proxy_url, self.ipv6_addr)

    def ensure_started(self) -> bool:
        if self.is_healthy():
            return False
        logger.warning("[IPv6Pool] proxy unhealthy, restarting: %s:%d", self.email, self.port)
        self.start()
        return True

    def _clear_runtime_refs(self) -> None:
        self._server = None
        self._loop = None
        self._thread = None
        self._main_task = None
        self._handler_tasks.clear()

    def stop(self) -> None:
        loop = self._loop
        server = self._server
        thread = self._thread

        if server and loop and loop.is_running():
            async def _shutdown() -> None:
                server.close()
                await server.wait_closed()
                for task in list(self._handler_tasks):
                    if not task.done():
                        task.cancel()
                if self._handler_tasks:
                    await asyncio.gather(*self._handler_tasks, return_exceptions=True)
                self._handler_tasks.clear()
                if self._main_task and not self._main_task.done():
                    self._main_task.cancel()

            future = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
            try:
                future.result(timeout=5)
            except Exception:
                pass

        if thread:
            thread.join(timeout=5)
            if thread.is_alive() and loop and loop.is_running():
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except Exception:
                    pass
                thread.join(timeout=2)

        _del_ipv6(self.ipv6_addr)
        self._clear_runtime_refs()
        self._ready.clear()
        logger.info("[IPv6Pool] stopped %s:%d (%s)", self.email, self.port, self.ipv6_addr)


class IPv6Pool:
    """Persistent manager mapping one email to one IPv6 proxy."""

    def __init__(self):
        self._entries: dict[str, _ProxyEntry] = {}
        self._used_ports: set[int] = set()
        self._loaded = False
        self._lock = threading.RLock()
        self._last_error = ""

    def is_enabled(self) -> bool:
        return ipv6_proxy_enabled()

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = _pool_file()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[IPv6Pool] load failed: %s", exc)
            return
        if not isinstance(data, list):
            return
        for item in data:
            if not isinstance(item, dict):
                continue
            email = _normalized_email(item.get("email"))
            ipv6_addr = str(item.get("ipv6_addr") or "").strip()
            port = int(item.get("port") or 0)
            if not email or not ipv6_addr or port <= 0:
                continue
            self._entries[email] = _ProxyEntry(email, ipv6_addr, port, item.get("created_at"))
            self._used_ports.add(port)

    def _save_locked(self) -> None:
        path = _pool_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [entry.to_dict() for entry in self._entries.values()]
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _find_free_port_locked(self) -> int:
        start = int(getattr(_config, "IPV6_PROXY_PORT_START", 30000) or 30000)
        end = int(getattr(_config, "IPV6_PROXY_PORT_END", 39999) or 39999)
        if end <= start:
            end = start + 1

        for port in range(start, end + 1):
            if port in self._used_ports:
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((_listen_host(), port))
                return port
            except OSError:
                continue
        raise RuntimeError("IPv6Pool has no free proxy port")

    def start(self, active_emails: set[str] | list[str] | None = None) -> None:
        if not self.is_enabled():
            logger.info("[IPv6Pool] disabled")
            return
        with self._lock:
            self._load_locked()
            if active_emails is not None:
                wanted = {_normalized_email(email) for email in active_emails if _normalized_email(email)}
                stale = [email for email in self._entries if email not in wanted]
                for email in stale:
                    entry = self._entries.pop(email)
                    self._used_ports.discard(entry.port)
                    entry.stop()
                if stale:
                    self._save_locked()
                    logger.info("[IPv6Pool] removed %d stale entries before startup", len(stale))
            for entry in list(self._entries.values()):
                try:
                    entry.start()
                except Exception as exc:
                    self._last_error = str(exc)
                    logger.warning("[IPv6Pool] restore failed for %s: %s", entry.email, exc)
            logger.info("[IPv6Pool] restored %d entries", len(self._entries))

    def assign(self, email: str) -> str | None:
        email_l = _normalized_email(email)
        if not email_l or not self.is_enabled():
            return None
        with self._lock:
            self._load_locked()
            entry = self._entries.get(email_l)
            if entry is not None:
                entry.ensure_started()
                return entry.proxy_url

            ipv6_addr = random_ipv6()
            port = self._find_free_port_locked()
            entry = _ProxyEntry(email_l, ipv6_addr, port)
            try:
                entry.start()
            except Exception as exc:
                self._last_error = str(exc)
                raise
            self._entries[email_l] = entry
            self._used_ports.add(port)
            self._save_locked()
            return entry.proxy_url

    def ensure(self, email: str) -> str | None:
        return self.assign(email)

    def get_proxy_url(self, email: str) -> str | None:
        email_l = _normalized_email(email)
        with self._lock:
            self._load_locked()
            entry = self._entries.get(email_l)
            return entry.proxy_url if entry else None

    def get_local_proxy_url(self, email: str) -> str | None:
        email_l = _normalized_email(email)
        with self._lock:
            self._load_locked()
            entry = self._entries.get(email_l)
            return entry.local_proxy_url if entry else None

    def get_playwright_proxy(self, email: str) -> dict[str, str] | None:
        local_url = self.get_local_proxy_url(email)
        return {"server": local_url} if local_url else None

    def release(self, email: str) -> bool:
        email_l = _normalized_email(email)
        if not email_l:
            return False
        with self._lock:
            self._load_locked()
            entry = self._entries.pop(email_l, None)
            if not entry:
                return False
            self._used_ports.discard(entry.port)
            entry.stop()
            self._save_locked()
            return True

    def ensure_active(self, active_emails: set[str] | list[str], *, create_missing: bool = False) -> int:
        if not self.is_enabled():
            return 0
        repaired = 0
        active = {_normalized_email(email) for email in active_emails if _normalized_email(email)}
        with self._lock:
            self._load_locked()
            stale = [email for email in self._entries if email not in active]
            for email in stale:
                entry = self._entries.pop(email)
                self._used_ports.discard(entry.port)
                entry.stop()
            for email in active:
                entry = self._entries.get(email)
                if entry is None:
                    if create_missing:
                        self.assign(email)
                    continue
                if entry.ensure_started():
                    repaired += 1
            if stale:
                self._save_locked()
                logger.info("[IPv6Pool] removed %d inactive entries", len(stale))
        return repaired

    def cleanup_expired(self) -> int:
        ttl = _max_ttl_seconds()
        if ttl <= 0:
            return 0
        now = time.time()
        removed = 0
        with self._lock:
            self._load_locked()
            expired = [email for email, entry in self._entries.items() if now - entry.created_at > ttl]
            for email in expired:
                entry = self._entries.pop(email)
                self._used_ports.discard(entry.port)
                entry.stop()
                removed += 1
            if removed:
                self._save_locked()
        return removed

    def cleanup_all(self, account_emails: set[str] | list[str]) -> int:
        active = {_normalized_email(email) for email in account_emails if _normalized_email(email)}
        removed = 0
        with self._lock:
            self._load_locked()
            stale = [email for email in self._entries if email not in active]
            for email in stale:
                entry = self._entries.pop(email)
                self._used_ports.discard(entry.port)
                entry.stop()
                removed += 1
            if removed:
                self._save_locked()
        removed += self.cleanup_expired()
        return removed

    def list_all(self) -> list[dict]:
        with self._lock:
            self._load_locked()
            return [
                {
                    **entry.to_dict(),
                    "proxy_url": entry.proxy_url,
                    "local_proxy_url": entry.local_proxy_url,
                    "healthy": entry.is_healthy(),
                }
                for entry in self._entries.values()
            ]

    def preflight(self) -> dict:
        required = bool(getattr(_config, "AUTOTEAM_IPV6_POOL_REQUIRED", False))
        enabled = self.is_enabled()
        prefix = str(getattr(_config, "IPV6_PREFIX", "") or "").strip()
        iface = str(getattr(_config, "IPV6_IFACE", "") or "").strip()
        start = int(getattr(_config, "IPV6_PROXY_PORT_START", 30000) or 30000)
        end = int(getattr(_config, "IPV6_PROXY_PORT_END", 39999) or 39999)
        ip_command_found = shutil.which("ip") is not None
        errors: list[str] = []
        warnings: list[str] = []

        if required and not enabled:
            errors.append("ipv6_required_but_disabled")
        if enabled and not prefix:
            errors.append("missing_ipv6_prefix")
        if enabled and not iface:
            errors.append("missing_ipv6_iface")
        if enabled and not ip_command_found:
            errors.append("missing_ip_command")
        if start <= 0 or end <= 0 or end < start:
            errors.append("invalid_port_range")
        if enabled and getattr(_config, "IPV6_PROXY_USE_SUDO", False) and shutil.which("sudo") is None:
            warnings.append("missing_sudo_command")

        return {
            "ok": not errors,
            "enabled": enabled,
            "required": required,
            "prefix_configured": bool(prefix),
            "iface_configured": bool(iface),
            "iface": iface,
            "ip_command_found": ip_command_found,
            "port_range": {"start": start, "end": end, "valid": start > 0 and end >= start},
            "pool_file": str(_pool_file()),
            "errors": errors,
            "warnings": warnings,
        }

    def status(self) -> dict:
        with self._lock:
            self._load_locked()
            entries = [
                {
                    **entry.to_dict(),
                    "proxy_url": entry.proxy_url,
                    "local_proxy_url": entry.local_proxy_url,
                    "healthy": entry.is_healthy(),
                }
                for entry in self._entries.values()
            ]
            used_ports = len(self._used_ports)
            preflight = self.preflight()

        start = preflight["port_range"]["start"]
        end = preflight["port_range"]["end"]
        port_capacity = max(0, end - start + 1) if preflight["port_range"]["valid"] else 0
        unhealthy_count = sum(1 for entry in entries if not entry.get("healthy"))
        ttl = _max_ttl_seconds()
        now = time.time()
        expired_count = (
            sum(1 for entry in entries if now - float(entry.get("created_at") or now) > ttl)
            if ttl > 0
            else 0
        )

        return {
            "enabled": preflight["enabled"],
            "required": preflight["required"],
            "ok": preflight["ok"] and unhealthy_count == 0,
            "count": len(entries),
            "unhealthy_count": unhealthy_count,
            "expired_count": expired_count,
            "used_ports": used_ports,
            "port_capacity": port_capacity,
            "port_usage_ratio": (used_ports / port_capacity) if port_capacity else 0.0,
            "last_error": self._last_error,
            "preflight": preflight,
            "entries": entries,
        }

    def stop_all(self) -> None:
        with self._lock:
            self._load_locked()
            for entry in list(self._entries.values()):
                entry.stop()


ipv6_pool = IPv6Pool()
