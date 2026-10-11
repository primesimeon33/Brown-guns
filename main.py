"""
Family Bonus Tracker — Discord Bot

Entry point. Wires together:
  * Submission-channel message parsing
  * Admin approval workflow (persistent views)
  * In-Discord bonus calculation + Excel export on admin request

Run:  python main.py
"""

import json
import logging
import os
from datetime import datetime
from io import BytesIO
from typing import Dict, List, Optional

import discord
from discord.ext import commands
from PIL import Image, UnidentifiedImageError

import config
from excel_exporter import build_export_workbook
from parsers import (
    Participant,
    build_participant_display,
    detect_event_type,
    extract_event_datetime,
    parse_participants,
)
from views import ApprovalView

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bonus_bot")


# ---------------------------------------------------------------------------
# Bot definition
# ---------------------------------------------------------------------------
class BonusBot(commands.Bot):
    """Main bot class holding the submissions store."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.reactions = True

        super().__init__(command_prefix="!", intents=intents, help_command=None)

        self._submissions: Dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def load_submissions(self) -> None:
        """Load pending submissions from the JSON store (called on startup)."""
        if not os.path.exists(config.SUBMISSIONS_STORE):
            self._submissions = {}
            return
        try:
            with open(config.SUBMISSIONS_STORE, "r", encoding="utf-8") as f:
                self._submissions = json.load(f)
            logger.info("Loaded %d submissions from store", len(self._submissions))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read submissions store: %s", exc)
            self._submissions = {}

    def save_submissions(self) -> None:
        """Persist the submissions store to disk."""
        try:
            with open(config.SUBMISSIONS_STORE, "w", encoding="utf-8") as f:
                json.dump(self._submissions, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.error("Could not write submissions store: %s", exc)

    def get_submission(self, submission_id: int) -> Optional[dict]:
        return self._submissions.get(str(submission_id))

    def add_submission(self, submission_id: int, data: dict) -> None:
        self._submissions[str(submission_id)] = data
        self.save_submissions()

    def remove_submission(self, submission_id: int) -> None:
        self._submissions.pop(str(submission_id), None)
        self.save_submissions()

    # ------------------------------------------------------------------
    # Approval side-effects
    # ------------------------------------------------------------------
    async def process_approval(self, submission: dict) -> None:
        """
        Hook for side-effects after an approval.

        Bonuses are calculated in the view (and persisted into the submission's
        participant dicts) before this is called. This method is the place to
        add future side-effects (e.g. posting a confirmation in the main chat).
        """
        logger.info(
            "Approved submission %s (%s) — %d participants",
            submission.get("submission_id"),
            submission.get("event_type"),
            len(submission.get("participants", [])),
        )

    async def process_rejection(self, submission: dict) -> None:
        """Notify the original submitter that their submission was rejected."""
        submitter_id = int(submission["submitter_id"])
        try:
            user = await self.fetch_user(submitter_id)
            await user.send(
                f"Hello! Your event bonus submission (event: **{submission['event_type']}**) "
                f"has been **rejected** by an admin.\n"
                f"If you believe this is a mistake, please contact the admin team."
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not DM submitter %d: %s", submitter_id, exc)

    # ------------------------------------------------------------------
    # View (re)registration
    # ------------------------------------------------------------------
    async def register_pending_views(self) -> None:
        """Re-add persistent views for every pending submissions after restart."""
        for sid_str, data in self._submissions.items():
            if data.get("status") != "pending":
                continue
            view = ApprovalView(submission_id=int(sid_str), bot=self)
            self.add_view(view)
            logger.info("Re-registered approval view for submission %s", sid_str)


# ---------------------------------------------------------------------------
# Bot instance
# ---------------------------------------------------------------------------
bot = BonusBot()


# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %d)", bot.user, bot.user.id)
    bot.load_submissions()
    await bot.register_pending_views()
    logger.info("Bonus bot is ready.")


@bot.event
async def on_error(event: str, *args, **kwargs) -> None:
    """Global error handler — log everything, never let an event crash the bot."""
    logger.exception("Unhandled exception in event '%s'", event)


@bot.event
async def on_message(message: discord.Message) -> None:
    # Ignore the bot's own messages and DMs.
    if message.author == bot.user or message.guild is None:
        return

    # Only process messages in the designated submission channel.
    if message.channel.id != config.SUBMISSION_CHANNEL_ID:
        await bot.process_commands(message)
        return

    # Wrap in try/except so a single malformed submission can never crash
    # the bot's event loop.
    try:
        await handle_submission(message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error processing submission %d: %s", message.id, exc)
        try:
            await message.add_reaction("❌")
            await message.reply(
                "⚠️ Something went wrong while processing your submission. "
                "Please contact an admin.",
                delete_after=30,
            )
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------
def _member_is_admin(member: discord.Member) -> bool:
    """Return True if the member has a configured admin role (or admin perm)."""
    if not config.ADMIN_ROLE_IDS:
        return member.guild_permissions.administrator
    member_role_ids = {role.id for role in member.roles}
    return bool(member_role_ids & set(config.ADMIN_ROLE_IDS))


@bot.command(name="export")
@commands.has_permissions(administrator=True)
async def export_bonuses(ctx: commands.Context) -> None:
    """
    Export all approved submissions to an Excel workbook.

    Produces two sheets:
      * Summary — per-user totals (kills, bonus, events attended)
      * Details — one row per approved participant

    Admin-only (configurable via ADMIN_ROLE_IDS).
    """
    if not isinstance(ctx.author, discord.Member) or not _member_is_admin(ctx.author):
        await ctx.reply("❌ You do not have permission to export bonuses.", delete_after=15)
        return

    submissions = list(bot._submissions.values())
    approved = [s for s in submissions if s.get("status") == "approved"]

    if not approved:
        await ctx.reply("ℹ️ There are no approved submissions to export yet.")
        return

    try:
        buf = build_export_workbook(submissions)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build export workbook: %s", exc)
        await ctx.reply(f"⚠️ Failed to generate the Excel file: {exc}")
        return

    filename = f"Family_Bonus_Export_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    file = discord.File(fp=buf, filename=filename)

    total_bonus = sum(
        int(p.get("bonus", 0))
        for s in approved
        for p in s.get("participants", [])
    )
    unique_users = len(
        {int(p["user_id"]) for s in approved for p in s.get("participants", [])}
    )

    await ctx.reply(
        f"📊 **Bonus Export**\n"
        f"Approved submissions: **{len(approved)}**\n"
        f"Unique users: **{unique_users}**\n"
        f"Total bonus paid out: **${total_bonus:,}**",
        file=file,
    )


# ---------------------------------------------------------------------------
# Submission handling
# ---------------------------------------------------------------------------
async def handle_submission(message: discord.Message) -> None:
    """Validate, parse, and forward a user submission to the admin channel."""
    content = message.content

    # ---- 1. Validate the screenshot attachment ----
    image_attachment = None
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            image_attachment = att
            break

    if image_attachment is None:
        await send_guidance(
            message,
            "Your submission is missing a screenshot.\n"
            "Please attach an image and try again.",
        )
        return

    # Verify the attachment is actually a readable image (rejects corrupt files
    # or files that merely claim an image content-type).
    if not await validate_image(image_attachment):
        await send_guidance(
            message,
            "The attached file does not appear to be a valid image.\n"
            "Please attach a valid screenshot (PNG/JPG/etc.).",
        )
        return

    # ---- 2. Detect event type ----
    event_type = detect_event_type(content)
    if event_type is None:
        await send_guidance(
            message,
            "I could not detect a supported event type in your message.\n"
            "Supported events: Informal, Biz War, State, Weapon Factory, "
            "Rating Battle, Foundry.",
        )
        return

    # ---- 3. Parse participants ----
    participants = parse_participants(content)
    if not participants:
        await send_guidance(
            message,
            "No participants were found.\n"
            "Please tag users followed by their kills, e.g. `@User 3k @User2 0k`.",
        )
        return

    # Resolve usernames from the guild for nicer display.
    await resolve_usernames(message.guild, participants)

    # ---- 4. Add the pending reaction ----
    await message.add_reaction("⏳")

    # ---- 5. Build the admin embed ----
    event_details = extract_event_datetime(content) or ""
    embed = build_admin_embed(
        message=message,
        event_type=event_type,
        event_details=event_details,
        image_url=image_attachment.url,
        participants=participants,
    )

    admin_channel = bot.get_channel(config.ADMIN_APPROVAL_CHANNEL_ID)
    if admin_channel is None:
        logger.error("Admin approval channel not found (ID %d).", config.ADMIN_APPROVAL_CHANNEL_ID)
        return

    view = ApprovalView(submission_id=message.id, bot=bot)
    admin_message = await admin_channel.send(embed=embed, view=view)

    # ---- 6. Persist the submission ----
    submission_data = {
        "submission_id": message.id,
        "channel_id": message.channel.id,
        "submitter_id": message.author.id,
        "submitter_name": str(message.author),
        "timestamp": message.created_at.strftime("%Y-%m-%d %H:%M"),
        "event_type": event_type,
        "event_details": event_details,
        "image_url": image_attachment.url,
        "participants": [p.to_dict() for p in participants],
        "admin_message_id": admin_message.id,
        "admin_channel_id": admin_channel.id,
        "status": "pending",
    }
    bot.add_submission(message.id, submission_data)

    logger.info(
        "New submission %d from %s (%s) — %d participants",
        message.id,
        message.author,
        event_type,
        len(participants),
    )


# ---------------------------------------------------------------------------
# Embed / display helpers
# ---------------------------------------------------------------------------
def build_admin_embed(
    *,
    message: discord.Message,
    event_type: str,
    event_details: str,
    image_url: str,
    participants: List[Participant],
) -> discord.Embed:
    """Construct the embed posted to the admin approval channel."""
    title = f"New Submission — {event_type}"
    if event_details:
        title += f" ({event_details})"

    embed = discord.Embed(
        title=title,
        color=discord.Color.blurple(),
        timestamp=message.created_at,
    )
    embed.set_author(
        name=f"Submitted by {message.author}",
        icon_url=message.author.display_avatar.url,
    )
    embed.add_field(
        name="Event Type",
        value=event_type,
        inline=True,
    )
    embed.add_field(
        name="Submitter",
        value=f"{message.author.mention}\nID: `{message.author.id}`",
        inline=True,
    )
    embed.add_field(
        name="Participants & Kills",
        value=build_participant_display(participants),
        inline=False,
    )
    embed.set_image(url=image_url)
    embed.set_footer(text="⏳ Pending approval")
    return embed


async def resolve_usernames(
    guild: Optional[discord.Guild], participants: List[Participant]
) -> None:
    """Fill in the username for each participant where possible."""
    if guild is None:
        return
    for p in participants:
        if p.username:
            continue
        member = guild.get_member(p.user_id)
        if member is not None:
            p.username = str(member)


async def validate_image(attachment: discord.Attachment) -> bool:
    """
    Download the attachment and confirm it opens as a valid image via Pillow.
    Returns False for corrupt or unreadable files.
    """
    try:
        data = await attachment.read()
        with Image.open(BytesIO(data)) as img:
            img.verify()  # raises on corrupt data
        return True
    except (UnidentifiedImageError, OSError, Exception) as exc:  # noqa: BLE001
        logger.warning("Image validation failed for %s: %s", attachment.filename, exc)
        return False


async def send_guidance(message: discord.Message, text: str) -> None:
    """Send a temporary guidance message and add a ❌ reaction to the submission."""
    await message.add_reaction("❌")
    try:
        await message.reply(
            f"⚠️ **Submission not processed**\n{text}\n\n"
            "**Expected format:**\n"
            "```\n"
            "<Event Type> [date time]\n"
            "@User1 3k @User2 0k @User3 12k\n"
            "[attached screenshot]\n"
            "```",
            delete_after=30,
        )
    except discord.Forbidden:
        logger.warning("Cannot reply in channel %d", message.channel.id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _validate_config() -> None:
    """Log warnings for any missing/zero configuration values."""
    checks = [
        ("DISCORD_BOT_TOKEN", bool(config.DISCORD_BOT_TOKEN)),
        ("SUBMISSION_CHANNEL_ID", config.SUBMISSION_CHANNEL_ID != 0),
        ("ADMIN_APPROVAL_CHANNEL_ID", config.ADMIN_APPROVAL_CHANNEL_ID != 0),
    ]
    for name, ok in checks:
        if not ok:
            logger.warning("⚠️  %s is not set or is zero — check your Railway env vars.", name)

    if not config.ADMIN_ROLE_IDS:
        logger.warning(
            "⚠️  ADMIN_ROLE_IDS is empty — falling back to server Administrator "
            "permission for approvals and the !export command."
        )


def main() -> None:
    _validate_config()

    if not config.DISCORD_BOT_TOKEN:
        logger.critical("DISCORD_BOT_TOKEN is not set. Set it in Railway variables.")
        raise SystemExit(1)

    try:
        bot.run(config.DISCORD_BOT_TOKEN)
    except discord.PrivilegedIntentsRequired:
        logger.critical(
            "Privileged intents required but not enabled. Go to the Discord "
            "Developer Portal → Bot → enable 'Message Content Intent' and "
            "'Server Members Intent'."
        )
        raise
    except Exception as exc:  # noqa: BLE001
        logger.critical("Bot failed to start: %s", exc, exc_info=True)
        raise


if __name__ == "__main__":
    main()
