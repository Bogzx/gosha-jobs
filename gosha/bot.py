"""Discord slash-command handlers."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from gosha.database import get_session
from gosha.models import Subscription, User

if TYPE_CHECKING:
    from gosha.config import Settings
    from gosha.ssh_tunnels import SSHTunnelManager

log = logging.getLogger(__name__)


def _chunk_text(text: str, max_chars: int = 3900) -> list[str]:
    """Split text into chunks ≤ max_chars, breaking on natural boundaries.

    Prefers paragraph breaks, then line breaks, then word boundaries; falls
    back to a hard cut if no separator exists in the second half of the window.
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        window = remaining[:max_chars]
        idx = max_chars  # hard-cut fallback
        for sep in ("\n\n", "\n", " "):
            candidate = window.rfind(sep)
            if candidate > max_chars // 2:
                idx = candidate
                break
        chunks.append(remaining[:idx].rstrip())
        remaining = remaining[idx:].lstrip()
    return chunks


class JobBot(commands.Bot):
    """Custom Bot subclass — syncs command tree on ready."""

    tunnel_manager: SSHTunnelManager | None = None
    alert_channel_id: int = 0
    settings: Settings | None = None  # Set by main.py
    _started_at: float = 0.0  # timestamp for uptime tracking
    _last_scrape_at: float = 0.0  # timestamp of last completed scrape

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        super().__init__(command_prefix="!", intents=intents)
        self._started_at = time.monotonic()

    async def setup_hook(self) -> None:
        # Register interaction handler so feedback buttons
        # work even after bot restart
        from gosha.views import setup_interaction_handler
        setup_interaction_handler(self)

        await self.add_cog(SubscriptionCog(self))
        await self.tree.sync()
        log.info("Slash commands synced")

        @self.tree.error
        async def on_app_command_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ) -> None:
            log.exception("Slash command error: %s", error)
            msg = "Something went wrong. Please try again later."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except discord.HTTPException:
                pass


class SubscriptionCog(commands.Cog):
    """Slash commands for managing job-search subscriptions."""

    def __init__(self, bot: JobBot) -> None:
        self.bot = bot

    # ── helpers ──────────────────────────────────────────────────

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        """Check if user is in ADMIN_DISCORD_IDS. Returns False and responds if not."""
        admin_ids = self.bot.settings.admin_user_ids if self.bot.settings else set()
        if admin_ids and interaction.user.id not in admin_ids:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True,
            )
            return False
        return True

    async def _get_or_create_user(self, session: Any, discord_id: int) -> User:
        result = await session.execute(
            select(User).where(User.discord_user_id == discord_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = User(discord_user_id=discord_id)
            session.add(user)
            await session.flush()
        return user

    # ── keyword autocomplete ────────────────────────────────────

    _KEYWORD_SUGGESTIONS = [
        ("computer science internship — 19 intern/junior roles", "computer science internship"),
        ("computer science — 18 general tech roles", "computer science"),
        ("cs entry level — 11 junior/graduate/trainee roles", "cs entry level"),
        ("tech internship — 9 tech + product + UX intern roles", "tech internship"),
        ("data science — 8 data/ML/AI roles", "data science"),
        ("software engineering — 9 dev roles (frontend, backend, etc.)", "software engineering"),
    ]

    async def _keyword_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        """Suggest smart keywords as the user types."""
        current_lower = current.lower()
        results = []
        for label, value in self._KEYWORD_SUGGESTIONS:
            if current_lower in label.lower() or current_lower in value.lower():
                results.append(app_commands.Choice(name=label[:100], value=value))
        # If the user typed something custom, include it as-is
        if current.strip() and not results:
            results.append(app_commands.Choice(name=current.strip(), value=current.strip()))
        return results[:25]

    # ── location autocomplete ───────────────────────────────────

    async def _location_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        """Suggest known locations as the user types."""
        from gosha.filters import LOCATION_ALIASES

        current_lower = current.lower().strip()
        seen: set[str] = set()
        results: list[app_commands.Choice[str]] = []
        for alias, info in LOCATION_ALIASES.items():
            display = info["search"]
            if display in seen:
                continue
            if current_lower in alias or current_lower in display.lower():
                results.append(app_commands.Choice(name=display, value=alias))
                seen.add(display)
        if current.strip() and not results:
            results.append(app_commands.Choice(name=current.strip(), value=current.strip()))
        return results[:25]

    # ── /subscribe ──────────────────────────────────────────────

    @app_commands.command(
        name="subscribe",
        description="Add a new job-search subscription",
    )
    @app_commands.describe(
        keyword="Search term — pick a smart keyword or type your own",
        location="City or country — start typing for suggestions",
        max_age_days="Only show jobs posted within this many days (default 7)",
        experience="Experience level filter (default: any)",
        exclude="Keywords to exclude, comma-separated. e.g. 'sales, marketing'",
        salary_min="Minimum annual salary (in local currency)",
    )
    @app_commands.choices(experience=[
        app_commands.Choice(name="Any level", value="any"),
        app_commands.Choice(name="Intern / Internship", value="intern"),
        app_commands.Choice(name="Junior / Entry Level", value="junior"),
        app_commands.Choice(name="Mid-level", value="mid"),
        app_commands.Choice(name="Senior+", value="senior"),
    ])
    async def subscribe(
        self,
        interaction: discord.Interaction,
        keyword: str,
        location: str,
        max_age_days: int = 7,
        experience: str = "any",
        exclude: str = "",
        salary_min: int | None = None,
    ) -> None:
        try:
            # Cap individual values to prevent abuse
            keywords = [k.strip()[:100] for k in keyword.split(",") if k.strip()][:20]
            locations = [loc.strip()[:100] for loc in location.split(",") if loc.strip()][:20]
            excluded = [e.strip()[:100] for e in exclude.split(",") if e.strip()][:20] if exclude else []
            exp_levels = [e.strip().lower()[:20] for e in experience.split(",") if e.strip()][:5]

            if not keywords or not locations:
                await interaction.response.send_message(
                    "Please provide at least one keyword and one location.",
                    ephemeral=True,
                )
                return

            if max_age_days < 1 or max_age_days > 90:
                await interaction.response.send_message(
                    "Max age must be between 1 and 90 days.",
                    ephemeral=True,
                )
                return

            if salary_min is not None and salary_min < 0:
                await interaction.response.send_message(
                    "Salary minimum can't be negative.",
                    ephemeral=True,
                )
                return

            async with get_session() as session:
                user = await self._get_or_create_user(session, interaction.user.id)
                limits = user.limits

                # Check subscription limit
                existing = await session.execute(
                    select(func.count(Subscription.id)).where(
                        Subscription.user_id == user.id,
                    )
                )
                current_count = existing.scalar() or 0
                max_subs = int(limits["max_subscriptions"])
                if current_count >= max_subs:
                    tier_name = user.tier.capitalize()
                    await interaction.response.send_message(
                        f"You've reached the **{tier_name}** limit of "
                        f"**{max_subs}** subscriptions. "
                        f"Remove one with `/unsubscribe` or upgrade your plan.",
                        ephemeral=True,
                    )
                    return

                # Enforce per-sub limits
                max_kw = int(limits["max_keywords_per_sub"])
                max_loc = int(limits["max_locations_per_sub"])
                if len(keywords) > max_kw:
                    tier_name = user.tier.capitalize()
                    await interaction.response.send_message(
                        f"Too many keywords ({len(keywords)}). **{tier_name}** plan allows "
                        f"**{max_kw}** per subscription. Use `/upgrade` for more.",
                        ephemeral=True,
                    )
                    return
                if len(locations) > max_loc:
                    tier_name = user.tier.capitalize()
                    await interaction.response.send_message(
                        f"Too many locations ({len(locations)}). **{tier_name}** plan allows "
                        f"**{max_loc}** per subscription. Use `/upgrade` for more.",
                        ephemeral=True,
                    )
                    return

                sub = Subscription(
                    user_id=user.id,
                    max_age_days=max_age_days,
                    salary_min=salary_min,
                    remote_ok=False,
                )
                sub.keywords = keywords
                sub.locations = locations
                sub.excluded_keywords = excluded
                sub.experience_levels = exp_levels
                session.add(sub)
                await session.commit()

                # Emit event
                try:
                    from gosha.events import emit_subscription_created
                    await emit_subscription_created(user.id, sub.id, keywords)
                except Exception:
                    pass  # Events are best-effort

                # Build a helpful confirmation showing what will happen
                from gosha.filters import expand_keyword

                kw_display = ", ".join(keywords)
                loc_display = ", ".join(locations)

                # Show expansion info if a smart keyword was used
                expanded = expand_keyword(keywords[0])
                expansion_note = ""
                if len(expanded) > 1:
                    expansion_note = (
                        f"\nThis will search **{len(expanded)} job titles** automatically "
                        f"(e.g. {', '.join(expanded[:3])}, ...)."
                    )

                # Show when jobs will arrive
                interval = self.bot.settings.scrape_interval_minutes if self.bot.settings else 60

                embed = discord.Embed(
                    title=f"Subscribed! #{sub.id}",
                    description=(
                        f"**Keywords:** {kw_display}\n"
                        f"**Location:** {loc_display}\n"
                        f"**Experience:** {experience}\n"
                        f"**Max age:** {max_age_days} days"
                        f"{expansion_note}"
                    ),
                    color=discord.Color.green(),
                )
                embed.set_footer(
                    text=f"Jobs are checked every {interval} min and sent to your DMs. "
                    f"Use /scrape_now to get results immediately."
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Error in /subscribe")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    @subscribe.autocomplete("keyword")
    async def _subscribe_keyword_ac(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._keyword_autocomplete(interaction, current)

    @subscribe.autocomplete("location")
    async def _subscribe_location_ac(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._location_autocomplete(interaction, current)

    # ── /unsubscribe ────────────────────────────────────────────

    @app_commands.command(
        name="unsubscribe",
        description="Remove one of your subscriptions by ID",
    )
    @app_commands.describe(id="Subscription ID shown by /my_searches")
    async def unsubscribe(self, interaction: discord.Interaction, id: int) -> None:
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Subscription)
                    .join(User)
                    .where(
                        Subscription.id == id,
                        User.discord_user_id == interaction.user.id,
                    )
                )
                sub = result.scalar_one_or_none()
                if sub is None:
                    await interaction.response.send_message(
                        "Subscription not found or you don't own it.",
                        ephemeral=True,
                    )
                    return

                user_id = sub.user_id
                await session.delete(sub)
                await session.commit()

                try:
                    from gosha.events import emit_subscription_deleted
                    await emit_subscription_deleted(user_id, id)
                except Exception:
                    pass

                await interaction.response.send_message(
                    f"Subscription **#{id}** removed.", ephemeral=True
                )
        except Exception:
            log.exception("Error in /unsubscribe")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /edit ───────────────────────────────────────────────────

    @app_commands.command(
        name="edit",
        description="Edit an existing subscription",
    )
    @app_commands.describe(
        id="Subscription ID to edit",
        keyword="New keyword(s), comma-separated (leave blank to keep)",
        location="New location(s), comma-separated (leave blank to keep)",
        max_age_days="New max age in days (0 to keep current)",
        experience="New experience level (leave blank to keep)",
        exclude="New exclusions, comma-separated (leave blank to keep)",
        blacklist="Blacklisted companies, comma-separated (leave blank to keep)",
        salary_min="New minimum salary (0 to remove, -1 to keep)",
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        id: int,
        keyword: str = "",
        location: str = "",
        max_age_days: int = 0,
        experience: str = "",
        exclude: str = "",
        blacklist: str = "",
        salary_min: int = -1,
    ) -> None:
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Subscription)
                    .join(User)
                    .where(
                        Subscription.id == id,
                        User.discord_user_id == interaction.user.id,
                    )
                )
                sub = result.scalar_one_or_none()
                if sub is None:
                    await interaction.response.send_message(
                        "Subscription not found or you don't own it.",
                        ephemeral=True,
                    )
                    return

                changes = []
                if keyword:
                    sub.keywords = [k.strip()[:100] for k in keyword.split(",") if k.strip()][:20]
                    changes.append(f"keywords={sub.keywords}")
                if location:
                    sub.locations = [loc.strip()[:100] for loc in location.split(",") if loc.strip()][:20]
                    changes.append(f"locations={sub.locations}")
                if max_age_days > 0:
                    sub.max_age_days = max_age_days
                    changes.append(f"max_age_days={max_age_days}")
                if experience:
                    sub.experience_levels = [e.strip().lower()[:20] for e in experience.split(",") if e.strip()][:5]
                    changes.append(f"experience={sub.experience_levels}")
                if exclude:
                    sub.excluded_keywords = [e.strip()[:100] for e in exclude.split(",") if e.strip()][:20]
                    changes.append(f"excluded={sub.excluded_keywords}")
                if blacklist:
                    sub.company_blacklist = [b.strip()[:100] for b in blacklist.split(",") if b.strip()][:20]
                    changes.append(f"blacklist={sub.company_blacklist}")
                if salary_min == 0:
                    sub.salary_min = None
                    changes.append("salary_min=removed")
                elif salary_min > 0:
                    sub.salary_min = salary_min
                    changes.append(f"salary_min={salary_min}")

                if not changes:
                    await interaction.response.send_message(
                        "No changes specified.", ephemeral=True
                    )
                    return

                await session.commit()
                await interaction.response.send_message(
                    f"Updated **#{id}**: {', '.join(changes)}",
                    ephemeral=True,
                )
        except Exception:
            log.exception("Error in /edit")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /pause & /resume ────────────────────────────────────────

    @app_commands.command(
        name="pause", description="Pause a subscription (stop receiving jobs)"
    )
    @app_commands.describe(id="Subscription ID to pause")
    async def pause(self, interaction: discord.Interaction, id: int) -> None:
        await self._set_active(interaction, id, False)

    @app_commands.command(
        name="resume", description="Resume a paused subscription"
    )
    @app_commands.describe(id="Subscription ID to resume")
    async def resume(self, interaction: discord.Interaction, id: int) -> None:
        await self._set_active(interaction, id, True)

    async def _set_active(
        self, interaction: discord.Interaction, sub_id: int, active: bool
    ) -> None:
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Subscription)
                    .join(User)
                    .where(
                        Subscription.id == sub_id,
                        User.discord_user_id == interaction.user.id,
                    )
                )
                sub = result.scalar_one_or_none()
                if sub is None:
                    await interaction.response.send_message(
                        "Subscription not found or you don't own it.",
                        ephemeral=True,
                    )
                    return

                sub.is_active = active
                await session.commit()

                if not active:
                    try:
                        from gosha.events import emit_subscription_paused
                        await emit_subscription_paused(sub.user_id, sub_id)
                    except Exception:
                        pass

                status = "resumed" if active else "paused"
                await interaction.response.send_message(
                    f"Subscription **#{sub_id}** {status}.", ephemeral=True
                )
        except Exception:
            log.exception("Error in /pause or /resume")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /my_searches ────────────────────────────────────────────

    @app_commands.command(
        name="my_searches",
        description="List your current job-search subscriptions",
    )
    async def my_searches(self, interaction: discord.Interaction) -> None:
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Subscription)
                    .join(User)
                    .where(User.discord_user_id == interaction.user.id)
                )
                subs = result.scalars().all()

            if not subs:
                await interaction.response.send_message(
                    "You have no subscriptions. Use `/subscribe` to create one!",
                    ephemeral=True,
                )
                return

            lines = []
            for s in subs:
                status = "active" if s.is_active else "paused"
                kw = ", ".join(s.keywords)
                loc = ", ".join(s.locations)
                line = f"**#{s.id}** [{status}] `{kw}` in `{loc}` (last {s.max_age_days}d)"
                extras = []
                if s.excluded_keywords:
                    extras.append(f"excl: {', '.join(s.excluded_keywords)}")
                if s.company_blacklist:
                    extras.append(f"blacklist: {', '.join(s.company_blacklist)}")
                if s.salary_min:
                    extras.append(f"salary>={s.salary_min}")
                exp = s.experience_levels
                if exp and exp != ["any"]:
                    extras.append(f"exp: {', '.join(exp)}")
                if s.remote_ok:
                    extras.append("+ remote")
                if extras:
                    line += f"\n  {' | '.join(extras)}"
                lines.append(line)

            desc_text = "\n".join(lines)
            if len(desc_text) > 4000:
                desc_text = desc_text[:4000] + "\n\n*... truncated*"
            embed = discord.Embed(
                title="Your Job Subscriptions",
                description=desc_text,
                color=discord.Color.blurple(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Error in /my_searches")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /scrape_now ─────────────────────────────────────────────

    _scrape_cooldown: dict[int, float] = {}  # discord_user_id -> last invocation timestamp

    @app_commands.command(
        name="scrape_now", description="Force an immediate scrape cycle"
    )
    async def scrape_now(self, interaction: discord.Interaction) -> None:
        from gosha.pipeline import run_scrape_cycle

        try:
            if not await self._require_admin(interaction):
                return

            if self.bot.tunnel_manager is None:
                await interaction.response.send_message(
                    "Bot not fully initialised yet.", ephemeral=True
                )
                return

            # Tier-based rate limiting
            async with get_session() as session:
                user_result = await session.execute(
                    select(User).where(User.discord_user_id == interaction.user.id)
                )
                user = user_result.scalar_one_or_none()
            cooldown_secs = int((user.limits if user else {}).get("scrape_now_cooldown", 600))

            now = time.monotonic()
            uid = interaction.user.id

            # Periodically clean up stale cooldown entries (older than 1 hour)
            if len(self._scrape_cooldown) > 50:
                cutoff = now - 3600
                self._scrape_cooldown = {
                    k: v for k, v in self._scrape_cooldown.items() if v > cutoff
                }

            last_used = self._scrape_cooldown.get(uid, 0.0)
            remaining = cooldown_secs - (now - last_used)
            if remaining > 0:
                await interaction.response.send_message(
                    f"Cooldown active — try again in {int(remaining)}s.",
                    ephemeral=True,
                )
                return
            self._scrape_cooldown[uid] = now

            await interaction.response.send_message(
                "Scrape started — I'll DM you when it's done.", ephemeral=True
            )

            # Pass semantic config from bot settings
            kwargs: dict = {
                "bot": self.bot,
                "tunnel_manager": self.bot.tunnel_manager,
                "alert_channel_id": self.bot.alert_channel_id,
            }
            if self.bot.settings:
                kwargs["use_semantic"] = self.bot.settings.use_semantic_matching
                kwargs["semantic_model"] = self.bot.settings.semantic_model
                kwargs["semantic_threshold"] = self.bot.settings.semantic_threshold

            async def _run_and_notify() -> None:
                try:
                    total = await run_scrape_cycle(**kwargs)
                    self.bot._last_scrape_at = time.monotonic()
                    await interaction.followup.send(
                        f"Scrape complete — **{total}** new jobs delivered.",
                        ephemeral=True,
                    )
                except Exception:
                    log.exception("Scrape cycle failed")
                    await interaction.followup.send(
                        "Scrape failed. Check bot logs for details.", ephemeral=True
                    )

            asyncio.create_task(_run_and_notify())
        except Exception:
            log.exception("Error in /scrape_now")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /stats ──────────────────────────────────────────────────

    @app_commands.command(
        name="stats", description="Show your job delivery statistics"
    )
    async def stats(self, interaction: discord.Interaction) -> None:
        from sqlalchemy import func

        from gosha.models import UserJob

        try:
            async with get_session() as session:
                user_result = await session.execute(
                    select(User).where(User.discord_user_id == interaction.user.id)
                )
                user = user_result.scalar_one_or_none()
                if user is None:
                    await interaction.response.send_message(
                        "No data yet. Subscribe to start receiving jobs!",
                        ephemeral=True,
                    )
                    return

                total = await session.execute(
                    select(func.count(UserJob.id)).where(UserJob.user_id == user.id)
                )
                total_count = total.scalar() or 0

                interested = await session.execute(
                    select(func.count(UserJob.id)).where(
                        UserJob.user_id == user.id,
                        UserJob.feedback == "interested",
                    )
                )
                interested_count = interested.scalar() or 0

                not_relevant = await session.execute(
                    select(func.count(UserJob.id)).where(
                        UserJob.user_id == user.id,
                        UserJob.feedback == "not_relevant",
                    )
                )
                not_relevant_count = not_relevant.scalar() or 0

                sub_count = await session.execute(
                    select(func.count(Subscription.id)).where(
                        Subscription.user_id == user.id,
                        Subscription.is_active.is_(True),
                    )
                )
                active_subs = sub_count.scalar() or 0

            embed = discord.Embed(
                title="Your Job Stats",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Active Subscriptions", value=str(active_subs), inline=True)
            embed.add_field(name="Jobs Delivered", value=str(total_count), inline=True)
            embed.add_field(name="Interested", value=str(interested_count), inline=True)
            embed.add_field(name="Not Relevant", value=str(not_relevant_count), inline=True)

            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Error in /stats")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /status ────────────────────────────────────────────────

    @app_commands.command(
        name="status", description="Check bot health and system status"
    )
    async def status(self, interaction: discord.Interaction) -> None:
        try:
            if not await self._require_admin(interaction):
                return

            # Uptime
            uptime_secs = int(time.monotonic() - self.bot._started_at)
            hours, remainder = divmod(uptime_secs, 3600)
            minutes, secs = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {secs}s"

            # Tunnels
            tunnel_mgr = self.bot.tunnel_manager
            if tunnel_mgr:
                proxies = tunnel_mgr.active_proxies()
                total_tunnels = len(tunnel_mgr._tunnels)
                tunnel_str = f"{len(proxies)}/{total_tunnels} active"
            else:
                tunnel_str = "No proxies configured"

            # Last scrape
            if self.bot._last_scrape_at > 0:
                ago = int(time.monotonic() - self.bot._last_scrape_at)
                last_scrape_str = f"{ago // 60}m {ago % 60}s ago"
            else:
                last_scrape_str = "Not yet"

            # Subscription count
            async with get_session() as session:
                sub_count = await session.execute(
                    select(func.count(Subscription.id)).where(
                        Subscription.is_active.is_(True)
                    )
                )
                active_subs = sub_count.scalar() or 0
                user_count = await session.execute(
                    select(func.count(User.id))
                )
                total_users = user_count.scalar() or 0

            # Scrape interval
            interval = self.bot.settings.scrape_interval_minutes if self.bot.settings else 60

            embed = discord.Embed(
                title="GOSHA Status",
                color=discord.Color.green(),
            )
            embed.add_field(name="Uptime", value=uptime_str, inline=True)
            embed.add_field(name="SSH Tunnels", value=tunnel_str, inline=True)
            embed.add_field(name="Last Scrape", value=last_scrape_str, inline=True)
            embed.add_field(name="Active Subscriptions", value=str(active_subs), inline=True)
            embed.add_field(name="Total Users", value=str(total_users), inline=True)
            embed.add_field(name="Scrape Interval", value=f"{interval}m", inline=True)

            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Error in /status")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /quickstart ────────────────────────────────────────────

    @app_commands.command(
        name="quickstart",
        description="Set up job alerts in one click (CS internships in your chosen city)",
    )
    @app_commands.describe(
        location="Your city — start typing for suggestions (default: Romania)",
    )
    async def quickstart(
        self,
        interaction: discord.Interaction,
        location: str = "romania",
    ) -> None:
        try:
            async with get_session() as session:
                user = await self._get_or_create_user(session, interaction.user.id)

                # Check if user already has subscriptions
                existing = await session.execute(
                    select(func.count(Subscription.id)).where(
                        Subscription.user_id == user.id,
                    )
                )
                if (existing.scalar() or 0) > 0:
                    await interaction.response.send_message(
                        "You already have subscriptions! Use `/my_searches` to view them, "
                        "or `/subscribe` to add more.",
                        ephemeral=True,
                    )
                    return

                sub = Subscription(
                    user_id=user.id,
                    max_age_days=14,
                    remote_ok=False,
                )
                sub.keywords = ["computer science internship"]
                sub.locations = [location.strip()]
                sub.excluded_keywords = []
                sub.company_blacklist = []
                sub.experience_levels = ["intern", "junior"]
                session.add(sub)
                await session.commit()

                try:
                    from gosha.events import emit_subscription_created
                    await emit_subscription_created(user.id, sub.id, sub.keywords)
                except Exception:
                    pass

            from gosha.filters import normalize_location

            _search_loc, _ = normalize_location(location.strip())
            interval = self.bot.settings.scrape_interval_minutes if self.bot.settings else 60

            embed = discord.Embed(
                title="You're all set!",
                description=(
                    f"Created subscription **#{sub.id}**:\n\n"
                    f"**Searching for:** CS internships & junior roles\n"
                    f"**Location:** {_search_loc}\n"
                    f"**Experience:** Intern + Junior\n"
                    f"**Looking back:** 14 days\n\n"
                    f"This searches **19 job titles** automatically across "
                    f"Indeed, LinkedIn, and Glassdoor.\n\n"
                    f"**Next steps:**\n"
                    f"- Run `/scrape_now` to get your first jobs immediately\n"
                    f"- Upload your CV with `/upload_cv` to enable AI cover letters\n"
                    f"- Use `/subscribe` to add more searches (up to 5)"
                ),
                color=discord.Color.green(),
            )
            embed.set_footer(
                text=f"Jobs arrive via DM every {interval} min. Make sure your DMs are open!"
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Error in /quickstart")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    @quickstart.autocomplete("location")
    async def _quickstart_location_ac(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._location_autocomplete(interaction, current)

    # ── /upgrade ───────────────────────────────────────────────

    @app_commands.command(
        name="upgrade",
        description="See your current plan and what Pro offers",
    )
    async def upgrade(self, interaction: discord.Interaction) -> None:
        try:
            async with get_session() as session:
                user = (await session.execute(
                    select(User).where(User.discord_user_id == interaction.user.id)
                )).scalar_one_or_none()

            tier = user.tier if user else "free"
            limits = user.limits if user else User().limits

            if tier == "free":
                embed = discord.Embed(
                    title="GOSHA Free vs Pro",
                    description="You're on the **Free** plan.",
                    color=discord.Color.gold(),
                )
                embed.add_field(
                    name="Free (current)",
                    value=(
                        f"- {limits['max_subscriptions']} subscriptions\n"
                        f"- {limits['max_applications']} tracked applications\n"
                        f"- Basic keyword matching\n"
                        f"- Indeed + LinkedIn + Glassdoor"
                    ),
                    inline=True,
                )
                embed.add_field(
                    name="Pro",
                    value=(
                        "- **15** subscriptions\n"
                        "- **Unlimited** application tracking\n"
                        "- **AI semantic matching** (smarter results)\n"
                        "- **Priority delivery** (get jobs first)\n"
                        "- **Email digests**\n"
                        "- **2 min** scrape cooldown"
                    ),
                    inline=True,
                )
                embed.set_footer(text="Contact the bot admin to upgrade.")
            else:
                embed = discord.Embed(
                    title=f"Your Plan: {tier.capitalize()}",
                    description=(
                        f"**Subscriptions:** {limits['max_subscriptions']}\n"
                        f"**Applications:** {'Unlimited' if int(limits['max_applications']) > 100 else limits['max_applications']}\n"
                        f"**Semantic matching:** {'Yes' if limits['semantic_matching'] else 'No'}\n"
                        f"**Priority delivery:** {'Yes' if limits['priority_delivery'] else 'No'}\n"
                        f"**Email delivery:** {'Yes' if limits['email_delivery'] else 'No'}\n"
                        f"**Scrape cooldown:** {int(limits['scrape_now_cooldown']) // 60} min"
                    ),
                    color=discord.Color.green(),
                )

            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Error in /upgrade")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /help ──────────────────────────────────────────────────

    @app_commands.command(
        name="help", description="Show all commands and how to use them"
    )
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="GOSHA — Job Matching Bot",
            description="I automatically scrape Indeed, LinkedIn & Glassdoor and DM you matching jobs.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="/quickstart [location]",
            value="One-click setup for CS internships. Best way to get started!",
            inline=False,
        )
        embed.add_field(
            name="/subscribe keyword location [options]",
            value=(
                "Create a custom subscription. Smart keywords:\n"
                "`computer science internship` — 19 intern/junior roles\n"
                "`computer science` — 18 general tech roles\n"
                "`cs entry level` — 11 junior/graduate roles\n"
                "`data science` — 8 data/ML/AI roles\n"
                "Or type any custom keyword."
            ),
            inline=False,
        )
        embed.add_field(
            name="/my_searches",
            value="List all your subscriptions with their IDs.",
            inline=True,
        )
        embed.add_field(
            name="/edit id [keyword] [location] ...",
            value="Modify an existing subscription.",
            inline=True,
        )
        embed.add_field(
            name="/unsubscribe id",
            value="Delete a subscription.",
            inline=True,
        )
        embed.add_field(
            name="/pause id  &  /resume id",
            value="Temporarily stop/restart a subscription.",
            inline=True,
        )
        embed.add_field(
            name="/scrape_now",
            value="Force an immediate scrape (5 min cooldown).",
            inline=True,
        )
        embed.add_field(
            name="/stats",
            value="See your delivery statistics.",
            inline=True,
        )
        embed.add_field(
            name="/apply job_id  &  /applications",
            value="Track where you've applied and your pipeline status.",
            inline=True,
        )
        embed.add_field(
            name="/upload_cv  &  /cover_letter job_id",
            value="Upload your CV once, then generate AI cover letters for any job.\nUse `/my_cv` to preview and `/delete_cv` to remove.",
            inline=True,
        )
        embed.add_field(
            name="/show_keywords  &  /upgrade",
            value="See keyword expansions or compare Free vs Pro.",
            inline=True,
        )
        embed.set_footer(
            text="Jobs arrive via DM — make sure your DMs are open! "
            "(Server Settings > Privacy > Allow DMs)"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /show_keywords ─────────────────────────────────────────

    @app_commands.command(
        name="show_keywords",
        description="See the exact job titles a smart keyword will search for",
    )
    @app_commands.describe(
        keyword="The keyword to expand — pick from the list or type your own",
    )
    async def show_keywords(
        self, interaction: discord.Interaction, keyword: str,
    ) -> None:
        from gosha.filters import expand_keyword

        terms = expand_keyword(keyword)

        if len(terms) == 1 and terms[0] == keyword:
            await interaction.response.send_message(
                f"`{keyword}` is not a smart keyword — it will be searched as-is on job boards.\n\n"
                f"**Smart keywords** that auto-expand:\n"
                + "\n".join(f"- `{k}`" for k in [
                    "computer science internship",
                    "computer science",
                    "cs entry level",
                    "tech internship",
                    "data science",
                    "software engineering",
                ]),
                ephemeral=True,
            )
            return

        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(terms))
        embed = discord.Embed(
            title=f"Keyword: {keyword}",
            description=f"This will search **{len(terms)} job titles**:\n\n{numbered}",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Each title is searched separately on Indeed, LinkedIn & Glassdoor.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @show_keywords.autocomplete("keyword")
    async def _show_keywords_ac(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._keyword_autocomplete(interaction, current)

    # ── /apply ─────────────────────────────────────────────────

    @app_commands.command(
        name="apply",
        description="Track a job application (mark a delivered job as 'applied')",
    )
    @app_commands.describe(
        job_id="The job ID from a job notification (shown in the embed footer)",
        notes="Optional notes (e.g. 'applied via company website')",
    )
    async def apply(
        self,
        interaction: discord.Interaction,
        job_id: int,
        notes: str = "",
    ) -> None:
        from gosha.models import Application, Job

        try:
            async with get_session() as session:
                user = (await session.execute(
                    select(User).where(User.discord_user_id == interaction.user.id)
                )).scalar_one_or_none()
                if not user:
                    await interaction.response.send_message(
                        "Subscribe first with `/quickstart` or `/subscribe`.",
                        ephemeral=True,
                    )
                    return

                # Verify job exists
                job = (await session.execute(
                    select(Job).where(Job.id == job_id)
                )).scalar_one_or_none()
                if not job:
                    await interaction.response.send_message(
                        f"Job #{job_id} not found.", ephemeral=True,
                    )
                    return

                # Check for duplicate
                existing = (await session.execute(
                    select(Application).where(
                        Application.user_id == user.id,
                        Application.job_id == job_id,
                    )
                )).scalar_one_or_none()
                if existing:
                    await interaction.response.send_message(
                        f"You already tracked this job (status: **{existing.status}**). "
                        f"Use `/update_application` to change the status.",
                        ephemeral=True,
                    )
                    return

                app = Application(
                    user_id=user.id,
                    job_id=job_id,
                    status="applied",
                    notes=notes or None,
                )
                session.add(app)
                await session.commit()

            embed = discord.Embed(
                title="Application tracked!",
                description=(
                    f"**{job.title}** at **{job.company}**\n"
                    f"Status: Applied\n"
                    + (f"Notes: {notes}" if notes else "")
                ),
                color=discord.Color.green(),
            )
            embed.set_footer(text="Use /applications to see all your tracked applications")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Error in /apply")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /update_application ────────────────────────────────────

    @app_commands.command(
        name="update_application",
        description="Update the status of a tracked application",
    )
    @app_commands.describe(
        job_id="The job ID to update",
        status="New status",
        notes="Optional notes (e.g. 'interview scheduled for Monday')",
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="Applied", value="applied"),
        app_commands.Choice(name="Phone Screen", value="phone_screen"),
        app_commands.Choice(name="Interview", value="interview"),
        app_commands.Choice(name="Offer!", value="offer"),
        app_commands.Choice(name="Rejected", value="rejected"),
        app_commands.Choice(name="Withdrawn", value="withdrawn"),
    ])
    async def update_application(
        self,
        interaction: discord.Interaction,
        job_id: int,
        status: str,
        notes: str = "",
    ) -> None:
        from gosha.models import Application, Job, _utcnow

        try:
            async with get_session() as session:
                user = (await session.execute(
                    select(User).where(User.discord_user_id == interaction.user.id)
                )).scalar_one_or_none()
                if not user:
                    await interaction.response.send_message(
                        "No applications found.", ephemeral=True,
                    )
                    return

                app = (await session.execute(
                    select(Application).where(
                        Application.user_id == user.id,
                        Application.job_id == job_id,
                    )
                )).scalar_one_or_none()
                if not app:
                    await interaction.response.send_message(
                        f"No application found for job #{job_id}. Use `/apply` first.",
                        ephemeral=True,
                    )
                    return

                old_status = app.status
                app.status = status
                app.updated_at = _utcnow()
                if notes:
                    app.notes = notes
                await session.commit()

                # Get job details for display
                job = (await session.execute(
                    select(Job).where(Job.id == job_id)
                )).scalar_one_or_none()
                title = job.title if job else f"Job #{job_id}"
                company = job.company if job else "Unknown"

            status_emoji = {
                "applied": ">>", "phone_screen": ">>",
                "interview": ">>", "offer": ">>",
                "rejected": ">>", "withdrawn": ">>",
            }.get(status, ">>")

            await interaction.response.send_message(
                f"{status_emoji} **{title}** at **{company}**: "
                f"{old_status} -> **{status}**"
                + (f"\nNotes: {notes}" if notes else ""),
                ephemeral=True,
            )
        except Exception:
            log.exception("Error in /update_application")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /applications ──────────────────────────────────────────

    @app_commands.command(
        name="applications",
        description="View all your tracked job applications",
    )
    async def applications(self, interaction: discord.Interaction) -> None:
        from gosha.models import Application, Job

        try:
            async with get_session() as session:
                user = (await session.execute(
                    select(User).where(User.discord_user_id == interaction.user.id)
                )).scalar_one_or_none()
                if not user:
                    await interaction.response.send_message(
                        "No applications yet. Use `/apply` after receiving a job!",
                        ephemeral=True,
                    )
                    return

                result = await session.execute(
                    select(Application, Job)
                    .join(Job, Application.job_id == Job.id)
                    .where(Application.user_id == user.id)
                    .order_by(Application.updated_at.desc())
                    .limit(25)
                )
                items = result.all()

            if not items:
                await interaction.response.send_message(
                    "No applications yet. When you get a job DM, use `/apply <job_id>` to start tracking!",
                    ephemeral=True,
                )
                return

            # Group by status
            by_status: dict[str, list[str]] = {}
            for app, job in items:
                line = f"**{job.title}** at {job.company} (#{job.id})"
                if app.notes:
                    line += f"\n  _{app.notes}_"
                by_status.setdefault(app.status, []).append(line)

            status_order = ["interview", "phone_screen", "applied", "offer", "rejected", "withdrawn"]
            status_labels = {
                "applied": "Applied",
                "phone_screen": "Phone Screen",
                "interview": "Interviewing",
                "offer": "Offers",
                "rejected": "Rejected",
                "withdrawn": "Withdrawn",
            }

            description_parts = []
            for s in status_order:
                if s in by_status:
                    label = status_labels.get(s, s)
                    entries = "\n".join(by_status[s])
                    description_parts.append(f"**{label}** ({len(by_status[s])})\n{entries}")

            desc_text = "\n\n".join(description_parts)
            if len(desc_text) > 4000:
                desc_text = desc_text[:4000] + "\n\n*... truncated. Use /update_application to see details.*"
            embed = discord.Embed(
                title=f"Your Applications ({len(items)})",
                description=desc_text,
                color=discord.Color.blue(),
            )
            embed.set_footer(text="Use /update_application <job_id> to change a status")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Error in /applications")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /upload_cv ─────────────────────────────────────────────

    @app_commands.command(
        name="upload_cv",
        description="Upload your CV (PDF, DOCX, or TXT) — ranks your job matches and powers cover letters",
    )
    @app_commands.describe(
        file="Your CV file (PDF, DOCX, or TXT — max 5 MB)",
        consent="True = you agree GOSHA may store your CV (asked once; see /privacy on the website)",
    )
    async def upload_cv(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        consent: bool | None = None,
    ) -> None:
        from gosha.cover_letter import extract_text_from_attachment
        from gosha.domain.errors import ConsentRequiredError
        from gosha.services import cv as cv_service

        try:
            # Validate file
            max_size = 5 * 1024 * 1024  # 5 MB
            if file.size > max_size:
                await interaction.response.send_message(
                    "File too large (max 5 MB).", ephemeral=True,
                )
                return

            allowed = (".pdf", ".txt", ".md", ".docx")
            if not any(file.filename.lower().endswith(ext) for ext in allowed):
                await interaction.response.send_message(
                    "Unsupported file type. Please upload a PDF, DOCX, or TXT file.",
                    ephemeral=True,
                )
                return

            async with get_session() as session:
                user = await self._get_or_create_user(session, interaction.user.id)
                await session.commit()

            # Consent before the file is read, same rule as the website.
            try:
                await cv_service.require_consent(user.id, consent is True)
            except ConsentRequiredError:
                await interaction.response.send_message(
                    "Before I store your CV I need your agreement:\n\n"
                    f"> {cv_service.CONSENT_TEXT}\n\n"
                    "Delete it any time with `/delete_cv`. To agree, run "
                    "`/upload_cv` again with **consent: True**. You only "
                    "need to do that once.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)

            # Extract text
            text = await extract_text_from_attachment(file)
            if not text or len(text.strip()) < 50:
                await interaction.followup.send(
                    "Could not extract enough text from your file. "
                    "Try uploading a .txt or .pdf with selectable text.",
                    ephemeral=True,
                )
                return

            # Same write path as the website: stores the text AND refreshes
            # the embedding that ranks this user's matches.
            await cv_service.store_cv_text(user.id, text)
            word_count = len(text.split())

            await interaction.followup.send(
                f"CV uploaded! ({word_count} words extracted from `{file.filename}`)\n\n"
                f"Your job matches are now ranked against it, and you can use "
                f"`/cover_letter <job_id>` to generate tailored cover letters.\n"
                f"Use `/my_cv` to preview or delete your stored CV.",
                ephemeral=True,
            )
        except Exception:
            log.exception("Error in /upload_cv")
            msg = "Something went wrong. Please try again later."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /my_cv ─────────────────────────────────────────────────

    @app_commands.command(
        name="my_cv",
        description="Preview or delete your stored CV",
    )
    async def my_cv(self, interaction: discord.Interaction) -> None:
        from gosha.cover_letter import get_monthly_usage, load_cv

        try:
            async with get_session() as session:
                user = (await session.execute(
                    select(User).where(User.discord_user_id == interaction.user.id)
                )).scalar_one_or_none()

            if not user:
                await interaction.response.send_message(
                    "No CV uploaded yet. Use `/upload_cv` to get started.",
                    ephemeral=True,
                )
                return

            cv_text = load_cv(user.id)
            if not cv_text:
                await interaction.response.send_message(
                    "No CV uploaded yet. Use `/upload_cv` to get started.",
                    ephemeral=True,
                )
                return

            usage = await get_monthly_usage(user.id)
            limit = int(user.limits.get("cover_letters_per_month", 3))
            word_count = len(cv_text.split())
            char_count = len(cv_text)

            # Paginate the full extracted CV across one or more embeds.
            # Each description must stay under 4096 chars — 3900 leaves room
            # for code-fence wrappers and a small safety margin.
            chunks = _chunk_text(cv_text, max_chars=3900)
            total = len(chunks)

            def _render(chunk: str) -> str:
                # Escape triple backticks so they can't close the code fence.
                return f"```\n{chunk.replace('`', chr(0x2019))}\n```"

            first = discord.Embed(
                title=(
                    "Your Stored CV"
                    if total == 1
                    else f"Your Stored CV — part 1/{total}"
                ),
                description=_render(chunks[0]),
                color=discord.Color.blue(),
            )
            first.add_field(name="Words", value=str(word_count), inline=True)
            first.add_field(name="Characters", value=str(char_count), inline=True)
            first.add_field(
                name="Cover Letters This Month",
                value=f"{usage}/{limit}" if limit < 100 else f"{usage} (unlimited)",
                inline=True,
            )
            first.set_footer(
                text="Use /upload_cv to replace, or /delete_cv to remove."
            )
            await interaction.response.send_message(embed=first, ephemeral=True)

            # Follow-up messages for any remaining chunks.
            for i, chunk in enumerate(chunks[1:], start=2):
                cont = discord.Embed(
                    title=f"Your Stored CV — part {i}/{total}",
                    description=_render(chunk),
                    color=discord.Color.blue(),
                )
                await interaction.followup.send(embed=cont, ephemeral=True)
        except Exception:
            log.exception("Error in /my_cv")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /delete_cv ─────────────────────────────────────────────

    @app_commands.command(
        name="delete_cv",
        description="Delete your stored CV",
    )
    async def delete_cv_cmd(self, interaction: discord.Interaction) -> None:
        from gosha.services.cv import delete_cv

        try:
            async with get_session() as session:
                user = (await session.execute(
                    select(User).where(User.discord_user_id == interaction.user.id)
                )).scalar_one_or_none()

            # The service also removes the CV embedding, every cover letter
            # generated from it, and the recorded consent — the storage-level
            # delete this used to call left all three behind.
            if user and await delete_cv(user.id):
                await interaction.response.send_message(
                    "CV deleted, along with its match vector and any cover "
                    "letters generated from it.", ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "No CV found to delete.", ephemeral=True,
                )
        except Exception:
            log.exception("Error in /delete_cv")
            msg = "Something went wrong. Please try again later."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)

    # ── /cover_letter ──────────────────────────────────────────

    @app_commands.command(
        name="cover_letter",
        description="Generate an AI cover letter for a specific job using your CV",
    )
    @app_commands.describe(
        job_id="The job ID (shown in job notification embeds)",
    )
    async def cover_letter(
        self, interaction: discord.Interaction, job_id: int,
    ) -> None:
        from gosha.cover_letter import (
            generate_cover_letter,
            get_monthly_usage,
            load_cv,
        )
        from gosha.models import Job

        try:
            async with get_session() as session:
                user = (await session.execute(
                    select(User).where(User.discord_user_id == interaction.user.id)
                )).scalar_one_or_none()

            if not user:
                await interaction.response.send_message(
                    "Use `/quickstart` or `/subscribe` first.", ephemeral=True,
                )
                return

            # Check CV exists
            if not load_cv(user.id):
                await interaction.response.send_message(
                    "Upload your CV first with `/upload_cv`.", ephemeral=True,
                )
                return

            # Check monthly limit
            usage = await get_monthly_usage(user.id)
            limit = int(user.limits.get("cover_letters_per_month", 3))
            if usage >= limit:
                await interaction.response.send_message(
                    f"You've used all **{limit}** cover letters this month.\n"
                    f"Use `/upgrade` to see Pro benefits (unlimited cover letters).",
                    ephemeral=True,
                )
                return

            # Verify job exists
            async with get_session() as session:
                job = (await session.execute(
                    select(Job).where(Job.id == job_id)
                )).scalar_one_or_none()
            if not job:
                await interaction.response.send_message(
                    f"Job #{job_id} not found.", ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)

            # Generate
            content, was_cached = await generate_cover_letter(user.id, job_id)
            if not content:
                await interaction.followup.send(
                    "Cover letter generation failed (AI service error). Please try again in a few minutes.",
                    ephemeral=True,
                )
                return

            used = 0 if was_cached else 1
            remaining = limit - usage - used if limit < 100 else None

            embed = discord.Embed(
                title=f"Cover Letter — {job.title} at {job.company}",
                description=content,
                color=discord.Color.purple(),
            )
            footer_parts = [f"Job #{job_id}"]
            if was_cached:
                footer_parts.append("cached")
            if remaining is not None:
                footer_parts.append(f"{remaining} left this month")
            embed.set_footer(text=" | ".join(footer_parts))

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Error in /cover_letter")
            msg = "Something went wrong. Please try again later."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
