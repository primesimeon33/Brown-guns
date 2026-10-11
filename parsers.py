"""
Parsing utilities for submission messages.

Handles:
  * Detecting the event type from the message header.
  * Extracting Discord mentions and the kill count associated with each.
  * Extracting the optional date/time stamp used by "Informal" events.
"""

import re
from dataclasses import dataclass
from typing import List, Optional

from config import EVENT_TYPES

# Matches a Discord user mention, e.g. <@123456> or <@!123456>
MENTION_RE = re.compile(r"<@!?(\d+)>")

# Matches a kill token: "3k", "3 k", "3K", "3 kills", "3 Kills", "0k".
# We pair each mention with the next kill token that follows it.
STANDALONE_KILLS_RE = re.compile(r"(\d+)\s*k(?:ills?)?", re.IGNORECASE)

# Matches date/time stamps like "11/10 18:00", "11-10 18:00", "11/10/26 18:00"
DATETIME_RE = re.compile(
    r"(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\s*(\d{1,2}:\d{2})?"
)


@dataclass
class Participant:
    user_id: int
    kills: int
    username: str = ""  # resolved later via API
    bonus: int = 0  # computed at approval time

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "kills": self.kills,
            "username": self.username,
            "bonus": self.bonus,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Participant":
        return cls(
            user_id=int(data["user_id"]),
            kills=int(data["kills"]),
            username=data.get("username", ""),
            bonus=int(data.get("bonus", 0)),
        )


def detect_event_type(content: str) -> Optional[str]:
    """
    Inspect the message content and return the canonical event type name,
    or None if no recognised event keyword is found.
    """
    lowered = content.lower()
    for canonical, aliases in EVENT_TYPES.items():
        for alias in aliases:
            # Use word-boundary-ish matching to avoid false positives
            if re.search(rf"(?:^|\W){re.escape(alias)}(?:\W|$)", lowered):
                return canonical
    return None


def extract_event_datetime(content: str) -> Optional[str]:
    """
    Pull out the date/time stamp (used mainly for Informal events).
    Returns a string like "11/10 18:00" or None.
    """
    match = DATETIME_RE.search(content)
    if not match:
        return None
    date_part = match.group(1)
    time_part = match.group(2) or ""
    return f"{date_part} {time_part}".strip()


def parse_participants(content: str) -> List[Participant]:
    """
    Parse every mention and its associated kill count.

    Strategy: find all mention positions, then for each mention locate the
    *next* kill token that appears after it. This tolerates messages where
    the mention and kill count are separated by other text.

    Participants without a recognisable kill count default to 0 kills.
    """
    mentions = list(MENTION_RE.finditer(content))
    if not mentions:
        return []

    # Precompute all kill-token positions so we can pair them efficiently.
    kill_tokens = list(STANDALONE_KILLS_RE.finditer(content))

    participants: List[Participant] = []
    for idx, mention_match in enumerate(mentions):
        user_id = int(mention_match.group(1))
        mention_end = mention_match.end()

        # Determine the search boundary: up to the next mention (or end of string).
        next_mention_start = (
            mentions[idx + 1].start() if idx + 1 < len(mentions) else len(content)
        )

        kills = 0
        for kt in kill_tokens:
            if kt.start() >= mention_end and kt.start() < next_mention_start:
                kills = int(kt.group(1))
                break

        participants.append(Participant(user_id=user_id, kills=kills))

    return participants


def build_participant_display(participants: List[Participant], show_bonus: bool = False) -> str:
    """Return a readable multi-line list of participants for embeds."""
    lines = []
    for p in participants:
        name = p.username or f"<@{p.user_id}>"
        line = f"• {name} — **{p.kills}** kills"
        if show_bonus and p.bonus:
            line += f" → **${p.bonus:,}**"
        lines.append(line)
    return "\n".join(lines) if lines else "_No participants detected._"
