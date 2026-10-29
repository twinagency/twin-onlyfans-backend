"""Conservative calendar resolution anchored to source time, never import time."""
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def day(value=None, tz=None):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00')) if value else datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    try:
        return stamp.astimezone(ZoneInfo(tz or 'UTC')).date()
    except (ValueError, ZoneInfoNotFoundError):
        return stamp.astimezone(timezone.utc).date()


def resolve(text, stated_at, tz=None):
    """Return inclusive date bounds and precision. Unsupported text stays uncertain."""
    anchor = day(stated_at, tz)
    text = text.strip().lower()
    exact = re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?:\s*(?:to|through|until|/)\s*(\d{4}-\d{2}-\d{2}))?', text)
    if exact:
        try:
            start, end = date.fromisoformat(exact[1]), date.fromisoformat(exact[2] or exact[1])
            if end < start:
                return None, None, 'uncertain'
            return start.isoformat(), end.isoformat(), 'day' if start == end else 'range'
        except ValueError:
            return None, None, 'uncertain'
    delta = {'today': 0, 'tonight': 0, 'tomorrow': 1, 'yesterday': -1}
    if text in delta:
        d = anchor + timedelta(days=delta[text])
        return d.isoformat(), d.isoformat(), 'day'
    match = re.fullmatch(r'in (\d{1,3}) days?', text)
    if match:
        d = anchor + timedelta(days=int(match[1]))
        return d.isoformat(), d.isoformat(), 'day'
    if text in {'next week', 'this week', 'last week'}:
        monday = anchor - timedelta(days=anchor.weekday())
        monday += timedelta(days={'next week': 7, 'this week': 0, 'last week': -7}[text])
        return monday.isoformat(), (monday + timedelta(days=6)).isoformat(), 'week'
    if text in {'next weekend', 'this weekend', 'last weekend'}:
        saturday = anchor - timedelta(days=anchor.weekday()) + timedelta(days=5)
        saturday += timedelta(days={'next weekend': 7, 'this weekend': 0, 'last weekend': -7}[text])
        return saturday.isoformat(), (saturday + timedelta(days=1)).isoformat(), 'weekend'
    if text in {'next month', 'this month', 'last month'}:
        serial = anchor.year * 12 + anchor.month - 1 + {'next month': 1, 'this month': 0, 'last month': -1}[text]
        y, m = divmod(serial, 12)
        return date(y, m+1, 1).isoformat(), date(y, m+1, monthrange(y, m+1)[1]).isoformat(), 'month'
    weekdays = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
    match = re.fullmatch(r'(next |this |last )?(' + '|'.join(weekdays) + ')', text)
    if match:
        offset = weekdays.index(match[2]) - anchor.weekday()
        if match[1] == 'last ':
            offset = offset if offset < 0 else offset - 7
        elif match[1] == 'next ':
            # Both colloquial readings are possible; preserve the week of uncertainty.
            first = anchor + timedelta(days=offset if offset > 0 else offset+7)
            return first.isoformat(), (first+timedelta(days=7)).isoformat(), 'ambiguous_weekday'
        elif match[1] != 'this ':
            offset %= 7
        d = anchor + timedelta(days=offset)
        return d.isoformat(), d.isoformat(), 'day'
    return None, None, 'uncertain'


def age_on(birth, today):
    born, today = date.fromisoformat(birth), day(today)
    if born > today:
        raise ValueError('birth date is in the future')
    # A Feb 29 birthday advances on March 1 in a non-leap year. No jurisdictional/legal claim.
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def age_range(age, stated_at, today):
    stated, today = day(stated_at), day(today)
    # DOB lies in (stated minus age+1 years, stated minus age years], inclusive days.
    def back(years):
        y = stated.year-years
        return stated.replace(year=y, day=min(stated.day, monthrange(y, stated.month)[1]))
    earliest, latest = back(int(age)+1)+timedelta(days=1), back(int(age))
    return age_on(latest.isoformat(), today), age_on(earliest.isoformat(), today)
