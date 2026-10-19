"""Тесты туннеля и мастера настройки (без сети)."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import setup_wizard
import tunnel


class FindUrlTests(unittest.TestCase):
    def test_cloudflared(self) -> None:
        line = "2026-10-19T10:00:00Z INF |  https://quiet-river-1234.trycloudflare.com                |"
        self.assertEqual(tunnel.find_url("cloudflared", line), "https://quiet-river-1234.trycloudflare.com")

    def test_tuna_with_colors(self) -> None:
        line = "\x1b[32mForwarding\x1b[0m https://4l7mqf-212-49-103-2.ru.tuna.am -> http://127.0.0.1:8080"
        self.assertEqual(tunnel.find_url("tuna", line), "https://4l7mqf-212-49-103-2.ru.tuna.am")

    def test_no_url(self) -> None:
        self.assertIsNone(tunnel.find_url("cloudflared", "INF Requesting new quick Tunnel on trycloudflare.com..."))


@unittest.skipIf(sys.platform == "win32", "фейковый бинарник — shell-скрипт")
class TunnelProcessTests(unittest.TestCase):
    def fake_binary(self, body: str) -> Path:
        path = Path(tempfile.mkdtemp()) / "cloudflared"
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        return path

    def run_tunnel(self, binary: Path) -> tuple[tunnel.Tunnel, str]:
        async def scenario():
            tun = tunnel.Tunnel("cloudflared", 8080)
            try:
                return tun, await tun.start(timeout=5)
            finally:
                await tun.stop()

        with mock.patch.object(tunnel, "find_binary", return_value=binary):
            return asyncio.run(scenario())

    def test_start_and_stop(self) -> None:
        binary = self.fake_binary('echo "starting"; echo "INF | https://abc-def.trycloudflare.com |"; sleep 30')
        tun, url = self.run_tunnel(binary)
        self.assertEqual(url, "https://abc-def.trycloudflare.com")
        self.assertIsNotNone(tun.proc.returncode)  # процесс остановлен

    def test_exit_without_url(self) -> None:
        binary = self.fake_binary('echo "ERR failed to connect"; exit 3')
        with self.assertRaises(tunnel.TunnelError) as ctx:
            self.run_tunnel(binary)
        self.assertIn("failed to connect", str(ctx.exception))

    def test_keeper_restarts_dropped_tunnel(self) -> None:
        counter = Path(tempfile.mkdtemp()) / "n"
        binary = self.fake_binary(
            f'n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}\n'
            'echo "INF | https://run-$n.trycloudflare.com |"\n'
            'if [ "$n" = 1 ]; then sleep 0.2; exit 1; fi\nsleep 30'
        )
        events: list[str] = []

        async def scenario():
            restored = asyncio.Event()

            async def on_lost():
                events.append("lost")

            async def on_restored(url):
                events.append(url)
                restored.set()

            keeper = tunnel.TunnelKeeper("cloudflared", 8080, on_lost=on_lost, on_restored=on_restored, retry_delay=0.01)
            try:
                events.append(await keeper.start())
                await asyncio.wait_for(restored.wait(), 5)
            finally:
                await keeper.stop()

        with mock.patch.object(tunnel, "find_binary", return_value=binary):
            asyncio.run(scenario())
        self.assertEqual(events, ["https://run-1.trycloudflare.com", "lost", "https://run-2.trycloudflare.com"])


class WizardTests(unittest.TestCase):
    def test_merge_env_keeps_comments(self) -> None:
        text = "# комментарий\nBOT_TOKEN=old\nCLUB_TZ=Europe/Samara\n"
        merged = setup_wizard.merge_env(text, {"BOT_TOKEN": "new", "ADMIN_IDS": "5"})
        self.assertEqual(merged, "# комментарий\nBOT_TOKEN=new\nCLUB_TZ=Europe/Samara\nADMIN_IDS=5\n")

    def test_run_writes_env(self) -> None:
        answers = iter(["123:abc", "1", ""])  # токен, Cloudflare, Vercel по умолчанию

        async def fake_admin(token):
            return 42, "Иван"

        async def fake_check(token):
            return "club_bot"

        env_path = Path(tempfile.mkdtemp()) / ".env"
        with mock.patch("builtins.input", lambda prompt="": next(answers)), \
                mock.patch.object(setup_wizard, "check_token", fake_check), \
                mock.patch.object(setup_wizard, "wait_for_admin", fake_admin), \
                mock.patch.object(setup_wizard, "ENV_PATH", env_path), \
                mock.patch.object(setup_wizard, "say"), \
                mock.patch.dict(os.environ):
            setup_wizard.run()
        env = dict(line.split("=", 1) for line in env_path.read_text(encoding="utf-8").splitlines())
        self.assertEqual(env["BOT_TOKEN"], "123:abc")
        self.assertEqual(env["ADMIN_IDS"], "42")
        self.assertEqual(env["TUNNEL"], "cloudflared")
        self.assertEqual(env["WEBAPP_URL"], setup_wizard.DEFAULT_WEBAPP)


if __name__ == "__main__":
    unittest.main()
