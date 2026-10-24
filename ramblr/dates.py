"""Resolve spoken due dates ('tomorrow', 'by Friday', 'next week') against the time the words were said."""
import re
from datetime import date, datetime, timedelta

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SAME_WEEKDAY = "same-weekday"
NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10, "a couple of": 2, "a few": 3}


def resolve(phrase, said_ts):
    """ISO date or None. Only phrases with an unambiguous meaning are resolved here."""
    p = " ".join((phrase or "").lower().split())
    if not p:
        return None
    base = datetime.fromtimestamp(said_ts).date()
    if re.search(r"\bday after tomorrow\b", p):
        return (base + timedelta(2)).isoformat()
    if re.search(r"\b(tomorrow|tmrw)\b", p):
        return (base + timedelta(1)).isoformat()
    if re.search(r"\b(today|tonight|this evening|this afternoon|this morning|by end of day|eod)\b", p):
        return base.isoformat()
    m = re.search(r"\bin (\d+|" + "|".join(NUMS) + r") (day|days|week|weeks)\b", p)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUMS[m.group(1)]
        return (base + timedelta(n * (7 if m.group(2).startswith("week") else 1))).isoformat()
    if re.search(r"\b(this weekend|over the weekend|the weekend)\b", p):
        return (base if base.weekday() >= 5 else base + timedelta(5 - base.weekday())).isoformat()
    if re.search(r"\bend of (the )?week\b", p):
        return (base + timedelta((4 - base.weekday()) % 7)).isoformat()
    if re.search(r"\bnext week\b", p):
        return (base + timedelta(7 - base.weekday())).isoformat()
    for i, d in enumerate(DAYS):
        if re.search(rf"\bnext {d}\b", p):
            # 'next Friday' = the Friday of next calendar week (Monday-based)
            return (base + timedelta(7 - base.weekday() + i)).isoformat()
        if re.search(rf"\b{d}\b", p):
            if i == base.weekday():
                return SAME_WEEKDAY      # 'Friday' said on a Friday: today or next week; the caller decides
            return (base + timedelta((i - base.weekday()) % 7)).isoformat()
    return None


def valid_iso(s, said_ts):
    """Accept a model-provided date only if it parses and is not before the day it was said."""
    try:
        d = date.fromisoformat(str(s)[:10])
    except ValueError:
        return None
    base = datetime.fromtimestamp(said_ts).date()
    return d.isoformat() if base <= d <= base + timedelta(400) else None
