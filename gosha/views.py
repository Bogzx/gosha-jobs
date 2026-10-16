"""Discord UI components — buttons, views, and interaction handlers."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord

log = logging.getLogger(__name__)


class JobFeedbackView(discord.ui.View):
    """View with Apply / Cover Letter / Interested / Not Relevant buttons.

    The custom_ids encode the UserJob and Job ids, and every click is
    handled by the global listener in setup_interaction_handler — which
    also works after a restart. The buttons deliberately have no callbacks
    of their own: discord.py dispatches a click both to a live view and to
    on_interaction, so per-button callbacks handled each click twice (two
    feedback writes, then InteractionResponded). Senders call stop() after
    sending, which also frees the view instead of keeping every DM's view
    in memory until restart.
    """

    def __init__(self, user_job_id: int, job_url: str | None = None, job_id: int | None = None) -> None:
        super().__init__(timeout=None)
        self.user_job_id = user_job_id

        # Apply button — a URL button that opens the job posting directly
        if job_url:
            self.add_item(discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="Apply",
                url=job_url,
                emoji="\U0001f4e8",
            ))

        # Cover Letter button — triggers AI generation
        if job_id is not None:
            self.add_item(discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="Cover Letter",
                custom_id=f"cover_letter:{job_id}:{user_job_id}",
                emoji="\U0001f4dd",
            ))

        self.add_item(discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Interested",
            custom_id=f"job_feedback:interested:{user_job_id}",
            emoji="\U0001f44d",
        ))
        self.add_item(discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Not Relevant",
            custom_id=f"job_feedback:not_relevant:{user_job_id}",
            emoji="\U0001f44e",
        ))


def _view_after_feedback(
    message: discord.Message | None, feedback: str,
) -> discord.ui.View:
    """The message's own buttons with the feedback pair disabled.

    Replacing the whole view with one disabled button took Apply and Cover
    Letter away the moment the user said they were interested.
    """
    label = "Interested" if feedback == "interested" else "Not Relevant"
    view = discord.ui.View.from_message(message, timeout=None) if message else discord.ui.View()
    for item in view.children:
        custom_id = getattr(item, "custom_id", None) or ""
        if custom_id.startswith("job_feedback:"):
            item.disabled = True  # type: ignore[attr-defined]
            if custom_id.split(":")[1] == feedback:
                item.label = f"Feedback: {label}"  # type: ignore[attr-defined]
    if not view.children:
        view.add_item(discord.ui.Button(label=f"Feedback: {label}", disabled=True))
    return view


async def _handle_feedback_interaction(
    interaction: discord.Interaction,
    feedback: str,
    user_job_id: int,
) -> None:
    """Shared handler for feedback button clicks (works for both fresh and persistent buttons)."""
    from gosha.feedback import record_feedback

    success = await record_feedback(user_job_id, feedback)
    if success:
        view = _view_after_feedback(interaction.message, feedback)
        try:
            await interaction.response.edit_message(view=view)
        except discord.HTTPException:
            pass
        finally:
            view.stop()  # clicks stay with on_interaction (see JobFeedbackView)
        log.info("Feedback %s recorded for UserJob %d", feedback, user_job_id)
    else:
        try:
            await interaction.response.send_message(
                "Could not record feedback.", ephemeral=True
            )
        except discord.HTTPException:
            pass


async def cover_letter_embed(user, job_id: int) -> discord.Embed | str:
    """A cover letter for `job_id` as an embed, or a message saying why not.

    Goes through gosha.services.cover_letters.generate_for_job, the same
    path as the website: it claims the quota slot before calling the LLM,
    so a double click or a click racing the website cannot generate past
    the monthly limit, and a second click while the first is generating
    is told so instead of starting another.
    """
    from gosha.cover_letter import get_monthly_usage
    from gosha.database import get_session
    from gosha.domain.errors import (
        DomainError,
        GenerationInProgressError,
        NoCvError,
        QuotaExceededError,
    )
    from gosha.models import Job
    from gosha.services.cover_letters import generate_for_job

    try:
        content, was_cached = await generate_for_job(user, job_id)
    except NoCvError:
        return "Upload your CV first with `/upload_cv`, then try again."
    except QuotaExceededError as exc:
        return f"{exc.message} Use `/upgrade` to see Pro benefits (unlimited cover letters)."
    except GenerationInProgressError:
        return "That letter is still being written — give it a few seconds."
    except DomainError as exc:
        return exc.message

    async with get_session() as session:
        job = await session.get(Job, job_id)
    title = job.title if job else f"Job #{job_id}"
    company = job.company if job else "Unknown"

    embed = discord.Embed(
        # Discord rejects titles over 256 characters, and the letter was
        # already generated (and counted) by then.
        title=f"Cover Letter — {title} at {company}"[:256],
        description=content[:4096],
        color=discord.Color.purple(),
    )
    footer_parts = [f"Job #{job_id}"]
    if was_cached:
        footer_parts.append("cached")
    limit = int(user.limits.get("cover_letters_per_month", 3))
    if limit < 100:
        remaining = max(0, limit - await get_monthly_usage(user.id))
        footer_parts.append(f"{remaining} left this month")
    embed.set_footer(text=" | ".join(footer_parts))
    return embed


async def _handle_cover_letter_interaction(
    interaction: discord.Interaction,
    job_id: int,
) -> None:
    """Handle the Cover Letter button click on job embeds."""
    from sqlalchemy import select

    from gosha.database import get_session
    from gosha.models import User

    try:
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with get_session() as session:
            user = (await session.execute(
                select(User).where(User.discord_user_id == interaction.user.id)
            )).scalar_one_or_none()

        if not user:
            await interaction.followup.send(
                "Use `/quickstart` first to set up your account.", ephemeral=True,
            )
            return

        result = await cover_letter_embed(user, job_id)
        if isinstance(result, str):
            await interaction.followup.send(result, ephemeral=True)
        else:
            await interaction.followup.send(embed=result, ephemeral=True)

    except Exception as exc:
        log.error("Cover letter button failed: %s", exc)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong generating the cover letter.", ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong generating the cover letter.", ephemeral=True,
                )
        except discord.HTTPException:
            pass


def setup_interaction_handler(bot: discord.Client) -> None:
    """Register an on_interaction listener that catches persistent button clicks.

    Handles both feedback buttons and cover letter buttons after bot restarts.
    Call this once in bot setup_hook.
    """
    @bot.event
    async def on_interaction(interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return

        custom_id = interaction.data.get("custom_id", "") if interaction.data else ""

        # Handle feedback buttons: "job_feedback:{feedback}:{user_job_id}"
        if custom_id.startswith("job_feedback:"):
            parts = custom_id.split(":")
            if len(parts) != 3:
                return
            feedback = parts[1]
            if feedback not in ("interested", "not_relevant"):
                return
            try:
                user_job_id = int(parts[2])
            except ValueError:
                return
            await _handle_feedback_interaction(interaction, feedback, user_job_id)
            return

        # Handle cover letter buttons: "cover_letter:{job_id}:{user_job_id}"
        if custom_id.startswith("cover_letter:"):
            parts = custom_id.split(":")
            if len(parts) != 3:
                return
            try:
                job_id = int(parts[1])
            except ValueError:
                return
            await _handle_cover_letter_interaction(interaction, job_id)
            return


def build_job_embed_with_buttons(
    job,
    user_job_id: int,
    relevance_score: float | None = None,
    match_info: str | None = None,
    scored_with_cv: bool = False,
) -> tuple[discord.Embed, JobFeedbackView]:
    """Build a Discord embed + feedback buttons for a job delivery.

    The score is a DM score (gosha.matching.dm_match_label) unless the
    user's CV was blended in, which keeps the cosine scale (match_label).

    Every value is whitespace-clean and non-empty (Discord refuses the
    whole message over one empty field). Returns (embed, view) tuple.
    """
    # Discord limits: title 256 chars, field value 1024 chars, description 4096 chars
    embed = discord.Embed(
        title=_clip(_one_line(job.title) or "Untitled role", 256),
        url=_web_url(job.url),
        color=discord.Color.green(),
    )
    embed.add_field(name="Company", value=_clip(_one_line(job.company) or "Unknown", 100), inline=True)
    embed.add_field(name="Location", value=_short_places(job.location), inline=True)
    embed.add_field(name="Salary", value=_salary(job), inline=True)
    embed.add_field(name="Posted", value=_posted(job), inline=True)

    description = _snippet(job.description, 300, job.title)
    if description:
        embed.add_field(name="Description", value=description, inline=False)

    from gosha.matching import dm_match_label, match_label

    label = (match_label if scored_with_cv else dm_match_label)(relevance_score)
    footer = f"Source: {job.source}" + (f" | {label}" if label else "") + f" | Job #{job.id}"
    if match_info:
        footer += f" | Matched: {match_info}"
    embed.set_footer(text=footer[:2048])

    from gosha.clicks import apply_url

    # The Apply button goes through the click-tracking redirect; the title
    # link stays the posting's own.
    view = JobFeedbackView(user_job_id, job_url=apply_url(user_job_id, job.url), job_id=job.id)
    return embed, view


# ── DM field formatting ───────────────────────────────────────────────

# The boards date postings in Romanian local time.
_BOARD_TZ = ZoneInfo("Europe/Bucharest")
_PERIOD_WORD = {
    "hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month",
    "yearly": "year", "annual": "year", "annually": "year",
}


def _one_line(text: str | None) -> str:
    return " ".join(str(text).split()) if text else ""


def _clip(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a word with an ellipsis."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;-–")
    return (cut or text[: limit - 1]) + "…"


def _web_url(url: str | None) -> str | None:
    """The posting's link, if Discord will take it: a bad one fails the DM."""
    url = (url or "").strip()
    return url if re.match(r"https?://[^\s]+$", url) else None


def _short_places(location: str | None, limit: int = 100) -> str:
    """"Cluj-Napoca, Iași, Sibiu +4 more" for a board listing many cities."""
    text = _one_line(location)
    if not text:
        return "N/A"
    if len(text) <= limit:
        return text
    places = [p.strip() for p in text.split(",") if p.strip()]
    shown: list[str] = []
    for i, place in enumerate(places):
        rest = f" +{len(places) - i} more"
        if len(", ".join([*shown, place])) + len(f" +{len(places) - i - 1} more") > limit:
            return ", ".join(shown) + rest if shown else _clip(text, limit)
        shown.append(place)
    return ", ".join(shown)


def _amount(value: float | None) -> str | None:
    return f"{value:,.0f}" if value else None


def _salary(job) -> str:
    """"4,500 – 6,500 EUR / month", with "≈ … RON / month" for other
    currencies or periods: "17 - 98 USD" (hourly) and "4,500 - 6,500 EUR"
    (monthly) used to look alike."""
    low, high = _amount(job.salary_min), _amount(job.salary_max)
    if not (low or high):
        return "Not listed"
    if low and high:
        text = low if low == high else f"{low} – {high}"
    else:
        text = f"from {low}" if low else f"up to {high}"
    currency = _one_line(job.salary_currency)
    if currency:
        text += f" {currency}"
    period = _PERIOD_WORD.get((job.salary_period or "").strip().lower())
    if period:
        text += f" / {period}"
    ron_low = _amount(getattr(job, "salary_monthly_min_ron", None))
    ron_high = _amount(getattr(job, "salary_monthly_max_ron", None))
    already_ron = currency.upper() == "RON" and period in (None, "month")
    if (ron_low or ron_high) and not already_ron:
        if ron_low and ron_high and ron_low != ron_high:
            ron = f"{ron_low} – {ron_high}"
        else:
            ron = ron_low or ron_high
        text += f"\n≈ {ron} RON / month"
    return text


def _days_ago(when: datetime | None) -> int | None:
    if when is None:
        return None
    if when.tzinfo is None:  # SQLite hands back naive UTC
        when = when.replace(tzinfo=timezone.utc)
    today = datetime.now(_BOARD_TZ).date()
    return max(0, (today - when.astimezone(_BOARD_TZ).date()).days)


def _posted(job) -> str:
    """"3 days ago" from the board's date; else when we first saw it."""
    days = _days_ago(job.posted_at)
    if days is not None:
        return "Today" if days == 0 else "Yesterday" if days == 1 else f"{days} days ago"
    days = _days_ago(job.first_seen_at)
    if days is None:
        return "Unknown"
    return "First seen today" if days == 0 else (
        "First seen yesterday" if days == 1 else f"First seen {days} days ago"
    )


# Invisible characters scraped along with the text (zero-width spaces,
# joiners, BOMs, soft hyphens).
_INVISIBLE_RE = re.compile("[\u00ad\u200b-\u200f\u2060\ufeff]")
_MARKDOWN_RE = re.compile(r"\*\*|__|[*`]|^\s*#+\s*|^\s*[-•·]\s+", re.M)
# Where the role itself is described, as headings in English and Romanian.
_ROLE_HEADING_RE = re.compile(
    r"(?im)^\s*(?:what you(?:'| wi)ll (?:do|be doing)|(?:key |your |main )?responsibilities|"
    r"(?:about )?the role|your role|role overview|job description|your tasks|tasks|"
    r"responsabilit[aăâ][tț]i|ce vei face|atribu[tț]ii|sarcini|descrierea (?:jobului|postului)|"
    r"rolul (?:t[aă]u|nostru)|despre (?:rol|job|post))\b[^\n]{0,30}?:?\s*$"
)
# Paragraphs about the company, not the role.
_ABOUT_RE = re.compile(
    r"(?i)^\s*(?:about (?!the role)|despre (?:noi|companie)|who we are|cine suntem|"
    r"our company|compania noastr[aă]|company description|descrierea companiei|location\b)"
)


def _snippet(description: str | None, limit: int, title: str | None = None) -> str:
    """The role's own description, plain: what follows a "What you'll do" /
    "Responsabilități" heading when there is one, else the text without
    its leading "About us" paragraphs or the title repeated. Production DMs
    opened with "**About MindMaze Therapeutics**"."""
    if not description:
        return ""
    text = _MARKDOWN_RE.sub("", _INVISIBLE_RE.sub("", str(description)))
    heading = _ROLE_HEADING_RE.search(text)
    if heading and text[heading.end():].strip():
        text = text[heading.end():]
    else:
        paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        title_key = _one_line(title).lower()
        while len(paragraphs) > 1 and (
            _ABOUT_RE.match(paragraphs[0])
            or _one_line(paragraphs[0].split("\n")[0]).lower() == title_key
        ):
            paragraphs.pop(0)
        text = "\n\n".join(paragraphs)
    text = _one_line(text)
    if not text:
        return ""
    return discord.utils.escape_markdown(_clip(text, limit))
