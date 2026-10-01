"""Discord UI components — buttons, views, and interaction handlers."""

from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)


class JobFeedbackView(discord.ui.View):
    """View with Apply / Interested / Not Relevant buttons on job embeds.

    The custom_id encodes the UserJob ID so the handler can record feedback
    even after a bot restart (handled by setup_interaction_handler).
    """

    def __init__(self, user_job_id: int, job_url: str | None = None, job_id: int | None = None) -> None:
        # timeout=None makes the view persistent (survives bot restarts)
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
        self._job_id: int | None = None
        if job_id is not None:
            cover_btn = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="Cover Letter",
                custom_id=f"cover_letter:{job_id}:{user_job_id}",
                emoji="\U0001f4dd",
            )
            cover_btn.callback = self._on_cover_letter
            self.add_item(cover_btn)
            self._job_id = job_id

        interested_btn = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Interested",
            custom_id=f"job_feedback:interested:{user_job_id}",
            emoji="\U0001f44d",
        )
        interested_btn.callback = self._on_interested

        not_relevant_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Not Relevant",
            custom_id=f"job_feedback:not_relevant:{user_job_id}",
            emoji="\U0001f44e",
        )
        not_relevant_btn.callback = self._on_not_relevant

        self.add_item(interested_btn)
        self.add_item(not_relevant_btn)

    async def _on_cover_letter(self, interaction: discord.Interaction) -> None:
        await _handle_cover_letter_interaction(interaction, self._job_id)

    async def _on_interested(self, interaction: discord.Interaction) -> None:
        await _handle_feedback_interaction(interaction, "interested", self.user_job_id)

    async def _on_not_relevant(self, interaction: discord.Interaction) -> None:
        await _handle_feedback_interaction(interaction, "not_relevant", self.user_job_id)


async def _handle_feedback_interaction(
    interaction: discord.Interaction,
    feedback: str,
    user_job_id: int,
) -> None:
    """Shared handler for feedback button clicks (works for both fresh and persistent buttons)."""
    from gosha.feedback import record_feedback

    success = await record_feedback(user_job_id, feedback)
    if success:
        label = "Interested" if feedback == "interested" else "Not Relevant"
        # Disable both buttons on the message
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            style=discord.ButtonStyle.success if feedback == "interested" else discord.ButtonStyle.secondary,
            label=f"Feedback: {label}",
            disabled=True,
        ))
        try:
            await interaction.response.edit_message(view=view)
        except discord.HTTPException:
            pass
        log.info("Feedback %s recorded for UserJob %d", feedback, user_job_id)
    else:
        try:
            await interaction.response.send_message(
                "Could not record feedback.", ephemeral=True
            )
        except discord.HTTPException:
            pass


async def _handle_cover_letter_interaction(
    interaction: discord.Interaction,
    job_id: int,
) -> None:
    """Handle the Cover Letter button click on job embeds."""
    from sqlalchemy import select

    from gosha.cover_letter import generate_cover_letter, get_monthly_usage, load_cv
    from gosha.database import get_session
    from gosha.models import Job, User

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

        if not load_cv(user.id):
            await interaction.followup.send(
                "Upload your CV first with `/upload_cv`, then try again.", ephemeral=True,
            )
            return

        # Check limit
        usage = await get_monthly_usage(user.id)
        limit = int(user.limits.get("cover_letters_per_month", 3))
        if usage >= limit:
            await interaction.followup.send(
                f"You've used all **{limit}** cover letters this month. "
                f"Use `/upgrade` to get unlimited.", ephemeral=True,
            )
            return

        content, was_cached = await generate_cover_letter(user.id, job_id)
        if not content:
            await interaction.followup.send(
                "Cover letter generation failed. Make sure you've uploaded your CV with `/upload_cv`.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            job = (await session.execute(
                select(Job).where(Job.id == job_id)
            )).scalar_one_or_none()

        title = job.title if job else f"Job #{job_id}"
        company = job.company if job else "Unknown"
        # Only subtract 1 from remaining if this was a fresh generation (not cached)
        used = 0 if was_cached else 1
        remaining = limit - usage - used if limit < 100 else None

        embed = discord.Embed(
            title=f"Cover Letter — {title} at {company}",
            description=content,
            color=discord.Color.purple(),
        )
        footer_parts = [f"Job #{job_id}"]
        if remaining is not None:
            footer_parts.append(f"{remaining} left this month")
        embed.set_footer(text=" | ".join(footer_parts))

        await interaction.followup.send(embed=embed, ephemeral=True)

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
