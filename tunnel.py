"""Публичный https-адрес для бота на домашнем ПК — без ручной настройки.

cloudflared: бесплатно, без регистрации, скачивается автоматически.
tuna (tuna.am): серверы в России, нужна бесплатная регистрация и токен.
"""

import asyncio
import contextlib
import logging
import os
import platform
import re
import shutil
import stat
import tarfile
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path

import config

log = logging.getLogger(__name__)

BIN_DIR = config.BASE_DIR / "bin"
CLOUDFLARED_DOWNLOAD = "https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
TUNA_INSTALL_HINT = (
    "Установите tuna: Windows — `winget install --id yuccastream.tuna`, "
    "Linux/macOS — `sh -c \"$(curl -sSLf https://releases.tuna.am/tuna/get.sh)\"`"
)

_URL_RE = {
    "cloudflared": re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com"),
    "tuna": re.compile(r"https://[a-z0-9.-]+\.tuna\.am"),
}
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class TunnelError(Exception):
    pass


def find_url(provider: str, line: str) -> str | None:
    match = _URL_RE[provider].search(_ANSI_RE.sub("", line))
    return match.group(0) if match else None


def _exe(name: str) -> str:
    return f"{name}.exe" if platform.system() == "Windows" else name


def find_binary(name: str) -> Path | None:
    local = BIN_DIR / _exe(name)
    if local.exists():
        return local
    found = shutil.which(name)
    return Path(found) if found else None


def _cloudflared_asset() -> str:
    arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "amd64"
    system = platform.system()
    if system == "Windows":
        return "cloudflared-windows-amd64.exe"
    if system == "Darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    return f"cloudflared-linux-{arch}"


def download_cloudflared() -> Path:
    """Скачивает официальный cloudflared с GitHub в папку bin/."""
    asset = _cloudflared_asset()
    BIN_DIR.mkdir(exist_ok=True)
    target = BIN_DIR / _exe("cloudflared")
    tmp = BIN_DIR / f"{asset}.part"
    log.info("Скачиваю cloudflared (%s)…", asset)
    urllib.request.urlretrieve(CLOUDFLARED_DOWNLOAD.format(asset=asset), tmp)
    if asset.endswith(".tgz"):
        with tarfile.open(tmp) as archive:
            member = archive.getmember("cloudflared")
            with archive.extractfile(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        tmp.unlink()
    else:
        tmp.replace(target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def resolve_provider() -> str:
    if config.TUNNEL != "auto":
        return config.TUNNEL
    return "tuna" if config.TUNA_TOKEN else "cloudflared"


class Tunnel:
    def __init__(self, provider: str, port: int) -> None:
        self.provider = provider
        self.port = port
        self.proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._tail: list[str] = []
        self._stopping = False
        self.dead = asyncio.Event()  # туннель работал и неожиданно упал

    async def _command(self) -> tuple[list[str], dict[str, str]]:
        env = dict(os.environ)
        if self.provider == "cloudflared":
            binary = find_binary("cloudflared") or await asyncio.to_thread(download_cloudflared)
            return [str(binary), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"], env
        if self.provider == "tuna":
            binary = find_binary("tuna")
            if binary is None:
                raise TunnelError(f"tuna не найдена. {TUNA_INSTALL_HINT}")
            if config.TUNA_TOKEN:
                env["TUNA_TOKEN"] = config.TUNA_TOKEN
            return [str(binary), "http", str(self.port)], env
        raise TunnelError(f"Неизвестный туннель TUNNEL={self.provider!r} (нужно cloudflared, tuna или off)")

    async def start(self, timeout: float = 60) -> str:
        cmd, env = await self._command()
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        url_found: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._reader = asyncio.create_task(self._read_output(url_found))
        try:
            return await asyncio.wait_for(asyncio.shield(url_found), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            await self.stop()
            raise TunnelError(f"{self.provider} не выдал адрес за {timeout:.0f} с:\n" + "\n".join(self._tail)) from None

    async def _read_output(self, url_found: asyncio.Future) -> None:
        """Читает вывод туннеля всё время его работы, чтобы не переполнился буфер."""
        assert self.proc and self.proc.stdout
        async for raw in self.proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            self._tail = (self._tail + [line])[-15:]
            if not url_found.done() and (url := find_url(self.provider, line)):
                url_found.set_result(url)
        code = await self.proc.wait()
        if not url_found.done():
            url_found.set_exception(TunnelError(
                f"{self.provider} завершился с кодом {code}:\n" + "\n".join(self._tail)
            ))
        elif not self._stopping:
            log.error("Туннель %s остановился (код %s):\n%s", self.provider, code, "\n".join(self._tail[-5:]))
            self.dead.set()

    async def stop(self) -> None:
        self._stopping = True
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except (TimeoutError, asyncio.TimeoutError):
                self.proc.kill()
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader


class TunnelKeeper:
    """Держит туннель живым: если процесс упал, поднимает новый с новым адресом."""

    def __init__(
        self, provider: str, port: int, *,
        on_lost: Callable[[], Awaitable[None]], on_restored: Callable[[str], Awaitable[None]],
        retry_delay: float = 5,
    ) -> None:
        self.provider = provider
        self.port = port
        self.on_lost = on_lost
        self.on_restored = on_restored
        self.retry_delay = retry_delay
        self.tunnel = Tunnel(provider, port)
        self._watch: asyncio.Task | None = None

    async def start(self) -> str:
        url = await self.tunnel.start()
        self._watch = asyncio.create_task(self._keep_alive())
        return url

    async def _keep_alive(self) -> None:
        delay = self.retry_delay
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            await self.tunnel.dead.wait()
            await self.on_lost()
            # Проработал меньше 2 минут — значит, проблема не разовая: ждём дольше.
            delay = min(delay * 2, 300) if loop.time() - started < 120 else self.retry_delay
            while True:
                await asyncio.sleep(delay)
                self.tunnel = Tunnel(self.provider, self.port)
                try:
                    url = await self.tunnel.start()
                    break
                except (TunnelError, OSError) as exc:
                    log.warning("Туннель пока не поднялся (%s), пробую снова через %s с", exc, delay)
                    delay = min(delay * 2, 300)
            log.info("Туннель снова работает: %s", url)
            await self.on_restored(url)

    async def stop(self) -> None:
        if self._watch:
            self._watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch
        await self.tunnel.stop()
