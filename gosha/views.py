"""Discord UI components — buttons, views, and interaction handlers."""

from __future__ import annotations

import logging

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
) -> tuple[discord.Embed, JobFeedbackView]:
    """Build a Discord embed + feedback buttons for a job delivery.

    Returns (embed, view) tuple.
    """
    # Discord limits: title 256 chars, field value 1024 chars, description 4096 chars
    title = (job.title[:253] + "...") if len(job.title or "") > 256 else job.title
    embed = discord.Embed(
        title=title,
        url=job.url,
        color=discord.Color.green(),
    )
    company = (job.company[:100] + "...") if len(job.company or "") > 100 else job.company
    embed.add_field(name="Company", value=company or "Unknown", inline=True)
    embed.add_field(name="Location", value=(job.location or "N/A")[:100], inline=True)

    if job.salary_min or job.salary_max:
        parts = []
        if job.salary_min:
            parts.append(f"{job.salary_min:,.0f}")
        if job.salary_max:
            parts.append(f"{job.salary_max:,.0f}")
        salary = " - ".join(parts)
        if job.salary_currency:
            salary += f" {job.salary_currency}"
        embed.add_field(name="Salary", value=salary, inline=True)
    else:
        embed.add_field(name="Salary", value="Not listed", inline=True)

    if job.description:
        desc = (
            job.description[:300] + "..."
            if len(job.description) > 300
            else job.description
        )
        embed.add_field(name="Description", value=desc, inline=False)

    from gosha.matching import match_label

    label = match_label(relevance_score)
    footer = f"Source: {job.source}" + (f" | {label}" if label else "") + f" | Job #{job.id}"
    if match_info:
        footer += f" | Matched: {match_info}"
    embed.set_footer(text=footer[:2048])

    view = JobFeedbackView(user_job_id, job_url=job.url, job_id=job.id)
    return embed, view
