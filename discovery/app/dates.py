"""A deliberately small, deterministic date grammar. No model decides dates."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo

WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')


@dataclass(frozen=True)
class DateFilter:
    semantic_text: str
    start: datetime
    end: datetime | None
    after_minute: int | None
    evening: bool
    timezone_name: str

    def matches(self, starts_at: datetime) -> bool:
        local = starts_at.astimezone(ZoneInfo(self.timezone_name))
        minute = local.hour * 60 + local.minute + local.second / 60 + local.microsecond / 60_000_000
        return (starts_at >= self.start and (self.end is None or starts_at < self.end)
                and (self.after_minute is None or minute > self.after_minute)
                and (not self.evening or minute >= 18 * 60))

    def as_dict(self):
        return {'from': self.start.isoformat(), 'until': self.end.isoformat() if self.end else None,
                'after_local_minute': self.after_minute, 'evening_from': '18:00' if self.evening else None,
                'timezone': self.timezone_name}


def parse_query(query: str, now: datetime, timezone_name: str = 'Europe/Dublin') -> DateFilter:
    if now.tzinfo is None:
        raise ValueError('now must include a timezone')
    zone = ZoneInfo(timezone_name)
    local = now.astimezone(zone)
    text = query.lower().strip()
    if not text:
        raise ValueError('Enter a search phrase.')
    selectors = re.findall(r'\b(?:tonight|today|tomorrow|this weekend|' + '|'.join(WEEKDAYS) + r')\b', text)
    if len(selectors) > 1:
        raise ValueError('Use one day expression at a time (for example Friday night).')
    # Do not silently reinterpret unsupported calendar phrases as semantic words.
    if re.search(r'\b(?:next|last)\s+(?:weekend|week|month|' + '|'.join(WEEKDAYS) + r')\b|\b\d{4}-\d{2}-\d{2}\b', text):
        raise ValueError('Supported dates: today, tonight, tomorrow, a weekday, this weekend.')
    if re.search(r'\b(?:morning|afternoon|yesterday|noon|midnight)\b', text):
        raise ValueError('This date grammar supports evening/night and after a time, not this time expression.')
    day, days = local.date(), 1
    evening = bool(re.search(r'\b(?:tonight|night|evening)\b', text))
    if selectors:
        selector = selectors[0]
        if selector == 'tomorrow':
            day += timedelta(days=1)
        elif selector in WEEKDAYS:
            day += timedelta(days=(WEEKDAYS.index(selector) - local.weekday()) % 7)
        elif selector == 'this weekend':
            # Saturday/Sunday means the current weekend, otherwise the coming one.
            day += timedelta(days=5 - local.weekday())
            days = 2
        text = re.sub(r'\b' + re.escape(selector) + r'\b', ' ', text)
    start = max(now, datetime.combine(day, time.min, zone)) if selectors else now
    end = datetime.combine(day + timedelta(days=days), time.min, zone) if selectors else None
    after = None
    match = re.search(r'\bafter\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', text)
    if match:
        hour, minute = int(match[1]), int(match[2] or 0)
        if not 1 <= hour <= 12 or not 0 <= minute <= 59:
            raise ValueError('Invalid time; use, for example, after 7pm or after 7:30pm.')
        after = (hour % 12 + (12 if match[3] == 'pm' else 0)) * 60 + minute
        text = text[:match.start()] + ' ' + text[match.end():]
    if re.search(r'\b(?:after|before|between|at)\s+\d|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b', text):
        raise ValueError('Supported time constraint: after 7pm (12-hour clock).')
    text = re.sub(r'\b(?:night|evening|on|at|something|please|find|me|with|the|a|an)\b', ' ', text)
    text = ' '.join(text.split())
    return DateFilter(text, start.astimezone(timezone.utc), end.astimezone(timezone.utc) if end else None,
                      after, evening, timezone_name)
