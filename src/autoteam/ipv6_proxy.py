"""Local SOCKS5 proxy with an IPv6 source address.

The module is intentionally inert unless IPv6 rotation is enabled in config.
It provides the low-level proxy primitive used by the persistent per-account
pool in ``ipv6_pool.py``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import random
import socket
import struct
import subprocess
import threading
from contextlib import contextmanager

from autoteam import config as _config

logger = logging.getLogger(__name__)


def ipv6_proxy_enabled() -> bool:
    return bool(getattr(_config, "AUTOTEAM_IPV6_POOL_ENABLED", False) and getattr(_config, "IPV6_PREFIX", ""))


def random_ipv6(prefix: str | None = None) -> str:
    raw_prefix = (prefix or getattr(_config, "IPV6_PREFIX", "") or "").strip()
    if not raw_prefix:
        raise RuntimeError("IPv6 prefix is not configured")

    network_text = raw_prefix if "/" in raw_prefix else f"{raw_prefix}::/64"
    network = ipaddress.IPv6Network(network_text, strict=False)
    if network.prefixlen > 64:
        raise RuntimeError(f"IPv6 prefix must be /64 or broader: {raw_prefix}")

    host_bits = 128 - network.prefixlen
    # Avoid the network address itself.
    offset = random.randint(1, (1 << host_bits) - 1)
    return str(ipaddress.IPv6Address(int(network.network_address) + offset))


def _ip_command_base() -> list[str]:
    if os.name == "nt":
        return ["ip"]
    if getattr(_config, "IPV6_PROXY_USE_SUDO", False):
        return ["sudo", "ip"]
    return ["ip"]


def _add_ipv6(addr: str, iface: str | None = None) -> bool:
    iface = (iface or getattr(_config, "IPV6_IFACE", "") or "").strip()
    if not iface:
        logger.warning("[IPv6] IPV6_IFACE is empty, cannot add %s", addr)
        return False

    try:
        subprocess.run(
            [*_ip_command_base(), "addr", "add", f"{addr}/64", "dev", iface, "nodad"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        logger.info("[IPv6] added address %s on %s", addr, iface)
        return True
    except Exception as exc:
        logger.warning("[IPv6] add address failed for %s on %s: %s", addr, iface, exc)
        return False


def _del_ipv6(addr: str, iface: str | None = None) -> bool:
    iface = (iface or getattr(_config, "IPV6_IFACE", "") or "").strip()
    if not iface:
        return False

    try:
        subprocess.run(
            [*_ip_command_base(), "addr", "del", f"{addr}/64", "dev", iface],
            capture_output=True,
            check=False,
            timeout=5,
        )
        logger.info("[IPv6] deleted address %s from %s", addr, iface)
        return True
    except Exception as exc:
        logger.warning("[IPv6] delete address failed for %s on %s: %s", addr, iface, exc)
        return False


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), timeout=300)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _connect_via_ipv6_source(
    dst_addr: str,
    dst_port: int,
    source_addr: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_event_loop()
    infos = await loop.getaddrinfo(dst_addr, dst_port, family=socket.AF_INET6, type=socket.SOCK_STREAM)
    if not infos:
        raise OSError("no IPv6 target address")

    af, socktype, proto, _canon, target = infos[0]
    sock = socket.socket(af, socktype, proto)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((source_addr, 0, 0, 0))
    sock.setblocking(False)
    await asyncio.wait_for(loop.sock_connect(sock, target), timeout=30)
    return await asyncio.open_connection(sock=sock)


async def _handle_socks5(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    source_addr: str,
) -> None:
    try:
        header = await asyncio.wait_for(client_reader.readexactly(2), timeout=30)
        version, nmethods = struct.unpack("!BB", header)
        if version != 5:
            return
        await client_reader.readexactly(nmethods)
        client_writer.write(b"\x05\x00")
        await client_writer.drain()

        request = await asyncio.wait_for(client_reader.readexactly(4), timeout=30)
        _version, command, _reserved, atyp = struct.unpack("!BBBB", request)
        if command != 1:
            client_writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
            await client_writer.drain()
            return

        if atyp == 1:
            dst_addr = socket.inet_ntoa(await client_reader.readexactly(4))
        elif atyp == 3:
            name_len = (await client_reader.readexactly(1))[0]
            dst_addr = (await client_reader.readexactly(name_len)).decode()
        elif atyp == 4:
            dst_addr = socket.inet_ntop(socket.AF_INET6, await client_reader.readexactly(16))
        else:
            client_writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
            await client_writer.drain()
            return

        dst_port = struct.unpack("!H", await client_reader.readexactly(2))[0]

        try:
            remote_reader, remote_writer = await _connect_via_ipv6_source(dst_addr, dst_port, source_addr)
        except (OSError, asyncio.TimeoutError):
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(dst_addr, dst_port),
                timeout=30,
            )

        client_writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
        await client_writer.drain()
        await asyncio.gather(_relay(client_reader, remote_writer), _relay(remote_reader, client_writer))
    except Exception:
        pass
    finally:
        try:
            client_writer.close()
        except Exception:
            pass


class IPv6Proxy:
    """One temporary local SOCKS5 proxy bound to a random IPv6 address."""

    def __init__(self, prefix: str | None = None, iface: str | None = None):
        self.prefix = prefix
        self.iface = iface
        self.ipv6_addr: str | None = None
        self.port: int | None = None
        self.proxy_url: str | None = None
        self._server: asyncio.base_events.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._main_task: asyncio.Task | None = None
        self._handler_tasks: set[asyncio.Task] = set()
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def start(self) -> str | None:
        if not ipv6_proxy_enabled():
            logger.info("[IPv6] proxy disabled")
            return None

        self.ipv6_addr = random_ipv6(self.prefix)
        self.port = _find_free_port()
        self.proxy_url = f"socks5://127.0.0.1:{self.port}"
        _add_ipv6(self.ipv6_addr, self.iface)

        self._loop = asyncio.new_event_loop()
        source_addr = self.ipv6_addr
        listen_port = self.port
        ready = self._ready

        async def _checked_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            if task is not None:
                self._handler_tasks.add(task)
            try:
                await _handle_socks5(reader, writer, source_addr)
            finally:
                if task is not None:
                    self._handler_tasks.discard(task)

        async def _run() -> None:
            server = await asyncio.start_server(_checked_handler, "127.0.0.1", listen_port)
            self._server = server
            ready.set()
            logger.info("[IPv6] SOCKS5 proxy started: %s -> %s", self.proxy_url, source_addr)
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

        self._thread = threading.Thread(target=_thread_main, daemon=True, name=f"ipv6proxy-{listen_port}")
        self._thread.start()
        self._ready.wait(timeout=10)
        if self._error:
            raise self._error
        if not self._server:
            raise RuntimeError(f"IPv6 proxy port {listen_port} startup timed out")
        return self.proxy_url

    def stop(self) -> None:
        loop = self._loop
        server = self._server

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

        if self._thread:
            self._thread.join(timeout=3)
            if self._thread.is_alive() and loop and loop.is_running():
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except Exception:
                    pass
                self._thread.join(timeout=2)

        if self.ipv6_addr:
            _del_ipv6(self.ipv6_addr, self.iface)

        self._server = None
        self._loop = None
        self._thread = None
        self._main_task = None
        self._handler_tasks.clear()
        self._ready.clear()
        logger.info("[IPv6] proxy stopped: %s", self.proxy_url or "")

    def get_playwright_proxy(self) -> dict[str, str] | None:
        if not self.proxy_url:
            return None
        return {"server": self.proxy_url}

    def __enter__(self) -> IPv6Proxy:
        self.start()
        return self

    def __exit__(self, *_args) -> None:
        self.stop()


@contextmanager
def ipv6_proxy(prefix: str | None = None, iface: str | None = None):
    proxy = IPv6Proxy(prefix=prefix, iface=iface)
    proxy.start()
    try:
        yield proxy
    finally:
        proxy.stop()
