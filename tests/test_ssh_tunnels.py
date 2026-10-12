"""The ssh command line behind the SOCKS5 proxy tunnels (gosha/ssh_tunnels.py)."""

from __future__ import annotations

from gosha.config import VPSConfig
from gosha.ssh_tunnels import DEFAULT_KNOWN_HOSTS_FILE, ssh_command

VPS = VPSConfig(
    host="100.110.49.82", user="gosha-proxy", local_port=1080,
    key_path="/home/app/.ssh/ap1.key", ssh_port=2255,
)


def _options(cmd: list[str]) -> dict[str, str]:
    """Every `-o Key=Value` pair, keyed by option name."""
    return dict(
        cmd[i + 1].split("=", 1) for i, arg in enumerate(cmd) if arg == "-o"
    )


def test_never_prompts_and_gives_up_on_a_dead_host(monkeypatch):
    monkeypatch.delenv("GOSHA_SSH_KNOWN_HOSTS_FILE", raising=False)
    opts = _options(ssh_command("/usr/bin/ssh", VPS))
    assert opts["BatchMode"] == "yes"
    assert opts["ConnectTimeout"] == "10"


def test_host_keys_are_pinned_in_a_writable_file(monkeypatch):
    """~/.ssh is mounted read-only, so with the default known_hosts
    `accept-new` could never save a key and every connect was a first use.
    New keys must go to the first file listed, under the data mount; keys
    pre-pinned in ~/.ssh/known_hosts must still be consulted."""
    monkeypatch.delenv("GOSHA_SSH_KNOWN_HOSTS_FILE", raising=False)
    opts = _options(ssh_command("/usr/bin/ssh", VPS))
    assert opts["StrictHostKeyChecking"] == "accept-new"
    assert opts["UpdateHostKeys"] == "no"
    assert opts["UserKnownHostsFile"] == (
        f'"{DEFAULT_KNOWN_HOSTS_FILE}" ~/.ssh/known_hosts'
    )
    assert DEFAULT_KNOWN_HOSTS_FILE.parent.name == "data"  # /app/data in the image


def test_known_hosts_file_can_be_moved(monkeypatch):
    monkeypatch.setenv("GOSHA_SSH_KNOWN_HOSTS_FILE", "/srv/gosha kh/known")
    opts = _options(ssh_command("/usr/bin/ssh", VPS))
    assert opts["UserKnownHostsFile"] == '"/srv/gosha kh/known" ~/.ssh/known_hosts'


def test_tunnel_target_and_ports(monkeypatch):
    monkeypatch.delenv("GOSHA_SSH_KNOWN_HOSTS_FILE", raising=False)
    cmd = ssh_command("/usr/bin/ssh", VPS)
    assert cmd[0] == "/usr/bin/ssh"
    assert cmd[cmd.index("-i") + 1] == "/home/app/.ssh/ap1.key"
    assert cmd[cmd.index("-p") + 1] == "2255"
    assert cmd[cmd.index("-D") + 1] == "1080"
    assert "-N" in cmd
    assert _options(cmd)["ExitOnForwardFailure"] == "yes"
    assert cmd[-1] == "gosha-proxy@100.110.49.82"
