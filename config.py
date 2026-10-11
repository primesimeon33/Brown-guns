"""
Centralized configuration for the Family Bonus Tracker bot.

All tunable values (rates, event definitions, channel IDs, credentials) live here
so they can be modified without touching the core logic.
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ---------------------------------------------------------------------------
# Discord configuration
# ---------------------------------------------------------------------------
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

SUBMISSION_CHANNEL_ID = int(os.getenv("SUBMISSION_CHANNEL_ID", "0"))
ADMIN_APPROVAL_CHANNEL_ID = int(os.getenv("ADMIN_APPROVAL_CHANNEL_ID", "0"))

# Role IDs permitted to approve / reject submissions
_admin_role_raw = os.getenv("ADMIN_ROLE_IDS", "")
ADMIN_ROLE_IDS = [
    int(rid.strip()) for rid in _admin_role_raw.split(",") if rid.strip()
]

# ---------------------------------------------------------------------------
# Bonus rate configuration
# ---------------------------------------------------------------------------
# Flat amount paid to every participant just for attending the event.
BASE_ATTENDANCE_RATE = 10_000

# Rate paid per kill, keyed by event type (canonical name must match EVENT_TYPES).
RATE_PER_KILL = {
    "Informal": 5_000,
    "Biz War": 10_000,
    "State": 7_000,
    "Weapon Factory": 8_000,
    "Rating Battle": 10_000,
    "Foundry": 6_000,
}

# ---------------------------------------------------------------------------
# Event type definitions
# ---------------------------------------------------------------------------
# Each entry maps a canonical display name to the list of keyword aliases that
# identify it inside a submission message (case-insensitive matching).
EVENT_TYPES = {
    "Informal": ["informal"],
    "Biz War": ["biz war", "bizwar", "biz-war"],
    "State": ["state"],
    "Weapon Factory": ["weapon factory", "weaponfactory", "weapon-factory", "wf"],
    "Rating Battle": ["rating battle", "ratingbattle", "rating-battle", "rating"],
    "Foundry": ["foundry"],
}

# ---------------------------------------------------------------------------
# Persistence file for submissions (survives bot restarts)
# ---------------------------------------------------------------------------
SUBMISSIONS_STORE = "submissions.json"

# Column headers used in the exported Excel "Details" sheet.
EXPORT_HEADERS = [
    "Date",
    "Event Type",
    "User ID",
    "Username / Discord Tag",
    "Kills Recorded",
    "Calculated Bonus Amount",
    "Status",
]
