"""One off-switch rule and one ID format for every alert path.

The bot (config.env_flag), the stdlib paths (alerts.alerts_enabled: the
heartbeat watchdog, scripts/site_watchdog.py), the smoke workflow's JS
page() and the site watchdog's shell gate must agree: blank = on, only
1/true/yes/on = on, anything else = off; `<@id>` is an ID everywhere.
The JS and shell are executed here, not just grepped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from gosha import alerts, config

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

ON = ["", "  ", "1", "true", "TRUE", " yes ", "On"]
OFF = ["0", "false", "False", " false ", "no", "off", "disabled", "nope", "t rue", "o n"]


@pytest.mark.parametrize("raw", ON + OFF)
def test_bot_and_stdlib_paths_agree(raw, monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", raw)
    expected = raw in ON
    assert alerts.alerts_enabled() is expected
    assert config.env_flag("ALERTS_ENABLED", default=True) is expected


def test_unset_means_on(monkeypatch):
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)
    assert alerts.alerts_enabled() is True
    assert config.env_flag("ALERTS_ENABLED", default=True) is True


def test_truthy_words_are_the_same_list():
    assert tuple(alerts.TRUTHY) == tuple(config.TRUTHY)


def test_post_dm_sync_honours_a_non_false_off_value(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "disabled")
    monkeypatch.setattr(alerts, "_discord_post", lambda *a, **k: pytest.fail("sent"))
    assert alerts.post_dm_sync("x", token="t", user_ids=[1]) is False


@pytest.mark.parametrize("raw,ids", [
    ("123", [123]),
    ("<@123>", [123]),
    ("<@!123>", [123]),
    (" <@123> , 456 ,Bogdanila", [123, 456]),
    ("123,<@123>", [123]),
    ("", []),
    ("!123", []),
    ("<@123", []),
    ("123>", []),
    ("<@ 123>", []),
    ("12 3", []),
])
def test_user_id_parsing_matches_config(raw, ids, monkeypatch):
    monkeypatch.setenv("ALERT_DM_USER_IDS", raw)
    assert alerts.parse_user_ids(raw) == ids
    assert config._parse_id_list("ALERT_DM_USER_IDS") == set(ids)


def test_heartbeat_path_dms_a_mention_formatted_id(monkeypatch):
    """The bug: `<@123>` worked in the bot and silently sent nothing here."""
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("ALERT_DM_USER_IDS", "<@123>")
    calls: list[tuple[str, dict]] = []

    def fake_post(path, token, payload, timeout):
        calls.append((path, payload))
        return {"id": "chan"}

    monkeypatch.setattr(alerts, "_discord_post", fake_post)
    assert alerts.post_dm_sync("hello") is True
    assert calls[0] == ("/users/@me/channels", {"recipient_id": "123"})


# ── site-watchdog.yml: the shell gate, executed ──────────────────────

def _gate_script() -> str:
    wf = yaml.safe_load((WORKFLOWS / "site-watchdog.yml").read_text())
    step = next(s for s in wf["jobs"]["watchdog"]["steps"] if s.get("id") == "config")
    return step["run"]


@pytest.mark.parametrize("raw", ON + OFF)
def test_site_watchdog_gate_follows_the_rule(raw, tmp_path):
    out = tmp_path / "out"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITHUB_OUTPUT": str(out),
        "DISCORD_TOKEN": "t",
        "ALERT_DM_USER_IDS": "123",
        "ALERTS_ENABLED": raw,
    }
    subprocess.run(["bash", "-e", "-c", _gate_script()], env=env, check=True,
                   capture_output=True, text=True)
    expected = "true" if raw in ON else "false"
    assert out.read_text().strip() == f"enabled={expected}"


def test_site_watchdog_gate_needs_both_secrets(tmp_path):
    out = tmp_path / "out"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GITHUB_OUTPUT": str(out),
           "DISCORD_TOKEN": "", "ALERT_DM_USER_IDS": "123", "ALERTS_ENABLED": ""}
    subprocess.run(["bash", "-e", "-c", _gate_script()], env=env, check=True,
                   capture_output=True, text=True)
    assert out.read_text().strip() == "enabled=false"


# ── scraper-smoke.yml: page() executed in node with mocks ────────────

HARNESS = r"""
const fs = require('fs');
const calls = [];
globalThis.fetch = async (url, opts) => {
  calls.push({ url, auth: opts.headers.Authorization, body: JSON.parse(opts.body) });
  return { ok: true, status: 200, json: async () => ({ id: 'chan-1' }) };
};
const noop = () => {};
const core = { notice: noop, warning: noop, info: noop };
const context = { repo: { owner: 'o', repo: 'r' }, serverUrl: 'https://github.com', runId: 7 };
const ok = async () => ({ data: {} });
const github = {
  rest: { issues: { listForRepo: async () => ({ data: [] }), create: ok,
                    createComment: ok, update: ok, listComments: ok } },
  paginate: async () => [],
};
const fakeFs = { readFileSync: (p) => p === 'smoke-result.json'
  ? JSON.stringify({ failing: ['bestjobs'] }) : 'log' };
const req = (m) => (m === 'fs' ? fakeFs : require(m));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const fn = new AsyncFunction('require', 'github', 'context', 'core',
                             fs.readFileSync(process.argv[2], 'utf8'));
fn(req, github, context, core).then(() => console.log(JSON.stringify(calls)));
"""


def _run_smoke_script(tmp_path: Path, **env_extra: str) -> list[dict]:
    wf = yaml.safe_load((WORKFLOWS / "scraper-smoke.yml").read_text())
    track = next(s for s in wf["jobs"]["smoke"]["steps"] if s.get("id") == "track")
    (tmp_path / "script.js").write_text(track["with"]["script"])
    (tmp_path / "harness.js").write_text(HARNESS)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OUTCOME": "failure",
           "DISCORD_BOT_TOKEN": "tok", "ALERT_DM_USER_IDS": "123", "ALERTS_ENABLED": ""}
    env.update(env_extra)
    result = subprocess.run(
        ["node", str(tmp_path / "harness.js"), str(tmp_path / "script.js")],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@needs_node
@pytest.mark.parametrize("raw", ON + OFF)
def test_smoke_page_follows_the_rule(raw, tmp_path):
    calls = _run_smoke_script(tmp_path, ALERTS_ENABLED=raw)
    if raw in ON:
        assert [c["url"].rsplit("/v10", 1)[1] for c in calls] == [
            "/users/@me/channels", "/channels/chan-1/messages"]
        assert calls[0]["auth"] == "Bot tok"
        assert calls[1]["body"]["allowed_mentions"] == {"parse": []}
    else:
        assert calls == []


ID_CASES = ["123", " <@123> ", "<@!123>", "!123", "<@123", "123>", "Bogdanila", "12 3", "<@!>"]


@needs_node
@pytest.mark.parametrize("token", ID_CASES)
def test_smoke_page_and_python_agree_on_every_id_form(token, tmp_path):
    calls = _run_smoke_script(tmp_path, ALERT_DM_USER_IDS=token)
    js = [int(c["body"]["recipient_id"]) for c in calls if c["url"].endswith("/channels")]
    assert js == alerts.parse_user_ids(token)


@needs_node
def test_smoke_page_accepts_mention_formatted_ids(tmp_path):
    calls = _run_smoke_script(tmp_path, ALERT_DM_USER_IDS="<@123>, <@!456>,123,Bogdanila")
    recipients = [c["body"]["recipient_id"] for c in calls if c["url"].endswith("/channels")]
    assert recipients == ["123", "456"]
