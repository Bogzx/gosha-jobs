"""/upgrade shows enforced limits only — no email digests, no priority."""

from __future__ import annotations

from types import SimpleNamespace

from gosha.bot import SubscriptionCog


async def test_upgrade_embed_promises_nothing_unimplemented(patched_db):
    sent: list = []

    async def send_message(content: str = "", **kwargs) -> None:
        sent.append(kwargs.get("embed"))

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=31337),  # no account yet -> free plan
        response=SimpleNamespace(send_message=send_message, is_done=lambda: True),
    )
    cog = SubscriptionCog(SimpleNamespace(settings=None))  # type: ignore[arg-type]

    await cog.upgrade.callback(cog, interaction)

    (embed,) = sent
    text = " ".join(
        [embed.title or "", embed.description or ""]
        + [f"{f.name} {f.value}" for f in embed.fields]
    ).lower()
    assert "free vs pro" in text
    assert "15 saved searches" in text
    for promise in ("email", "digest", "priority", "semantic"):
        assert promise not in text, promise
