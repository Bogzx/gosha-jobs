"""Manage background SSH SOCKS5 tunnel processes."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass

from gosha.config import VPSConfig

log = logging.getLogger(__name__)


def ssh_command(ssh_bin: str, vps: VPSConfig) -> list[str]:
    """The ssh invocation that opens a SOCKS5 tunnel to one VPS."""
    return [
        ssh_bin,
        "-i", vps.key_path,
        "-p", str(vps.ssh_port),
        "-D", str(vps.local_port),
        "-N", "-q",
        "-o", "StrictHostKeyChecking=accept-new",
        # ~/.ssh is mounted read-only; don't try to rewrite known_hosts.
        "-o", "UpdateHostKeys=no",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        f"{vps.user}@{vps.host}",
    ]


@dataclass
class _Tunnel:
    """Internal bookkeeping for a single tunnel."""

    vps: VPSConfig
    process: asyncio.subprocess.Process | None = None


class SSHTunnelManager:
    """Spin up / tear down SSH dynamic-port-forwarding tunnels."""

    def __init__(self, vps_list: list[VPSConfig]) -> None:
        self._tunnels: list[_Tunnel] = [_Tunnel(vps=v) for v in vps_list]

    async def start_all(self) -> None:
        """Open an SSH SOCKS5 tunnel for every configured VPS."""
        ssh_bin = shutil.which("ssh")
        if ssh_bin is None:
            raise RuntimeError("ssh binary not found — install openssh-client")

        for t in self._tunnels:
            cmd = ssh_command(ssh_bin, t.vps)
            log.info(
                "Opening tunnel %s@%s:%d -> 127.0.0.1:%d",
                t.vps.user, t.vps.host, t.vps.ssh_port, t.vps.local_port,
            )
            t.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(1)
            if t.process.returncode is not None:
                stderr = (
                    (await t.process.stderr.read()).decode()
                    if t.process.stderr
                    else ""
                )
                log.error("Tunnel to %s exited immediately: %s", t.vps.host, stderr)
                t.process = None
            else:
                log.info("Tunnel active on 127.0.0.1:%d", t.vps.local_port)

    async def stop_all(self) -> None:
        """Terminate every running tunnel process."""
        for t in self._tunnels:
            if t.process and t.process.returncode is None:
                log.info("Closing tunnel 127.0.0.1:%d", t.vps.local_port)
                t.process.terminate()
                try:
                    await asyncio.wait_for(t.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    t.process.kill()
                t.process = None

    def active_proxies(self) -> list[str]:
        """Return SOCKS5 proxy URLs for all currently alive tunnels."""
        return [
            f"socks5://127.0.0.1:{t.vps.local_port}"
            for t in self._tunnels
            if t.process and t.process.returncode is None
        ]

    async def check_and_restart(self) -> int:
        """Restart any tunnels that have died. Returns the number restarted."""
        ssh_bin = shutil.which("ssh")
        if ssh_bin is None:
            return 0

        restarted = 0
        for t in self._tunnels:
            if t.process is not None and t.process.returncode is None:
                continue  # Still alive
            if t.process is not None:
                log.warning(
                    "Tunnel to %s died (exit=%s) — restarting",
                    t.vps.host,
                    t.process.returncode,
                )
            cmd = ssh_command(ssh_bin, t.vps)
            t.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(1)
            if t.process.returncode is not None:
                stderr = (
                    (await t.process.stderr.read()).decode()
                    if t.process.stderr
                    else ""
                )
                log.error("Restart failed for %s: %s", t.vps.host, stderr)
                t.process = None
            else:
                log.info("Restarted tunnel on 127.0.0.1:%d", t.vps.local_port)
                restarted += 1
        return restarted
