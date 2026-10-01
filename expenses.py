"""Pure helpers for the expense bot: parsing, date ranges, filtering and summaries.

Nothing in here talks to Telegram or Google Sheets, so it can be unit-tested.
"""
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

# An amount is the first number in the message. Thousands separators are allowed
# in both western (1,200,000) and Indian (1,20,000) grouping, plus up to 2 decimals.
AMOUNT_RE = re.compile(r'(?<![\d.])(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d{1,2}))?(?![\d,])')

COLUMNS = ("ts", "amount", "reason", "category", "notes", "user")

MONTHS = {
    'january': 1, 'jan': 1, 'february': 2, 'feb': 2, 'march': 3, 'mar': 3,
    'april': 4, 'apr': 4, 'may': 5, 'june': 6, 'jun': 6,
    'july': 7, 'jul': 7, 'august': 8, 'aug': 8, 'september': 9, 'sep': 9, 'sept': 9,
    'october': 10, 'oct': 10, 'november': 11, 'nov': 11, 'december': 12, 'dec': 12,
}


def parse_amount(text: str):
    """Return (amount_string, match) for the first amount in text, or (None, None).

    Separators are removed: "1,200.50" -> "1200.50".
    """
    m = AMOUNT_RE.search(text)
    if not m:
        return None, None
    whole = m.group(1).replace(",", "")
    amount = f"{whole}.{m.group(2)}" if m.group(2) else whole
    return amount, m


def parse_message(text: str):
    """Parse '<amount> <description> #category @notes' into (amount, reason, category, notes)."""
    if not text:
        return None, None, None, None

    if "#" in text:
        before_hash, after_hash = text.split("#", 1)
        if "@" in after_hash:
            category, notes = after_hash.split("@", 1)
            category = category.strip()
            notes = notes.strip()
        else:
            category = after_hash.strip()
            notes = None
    else:
        before_hash = text
        category = "misc"
        notes = None
        if "@" in before_hash:
            before_at, after_at = before_hash.split("@", 1)
            before_hash = before_at.strip()
            notes = after_at.strip()

    before_hash = before_hash.strip()
    amount, m = parse_amount(before_hash)
    if not amount:
        return None, None, category, notes

    reason = (before_hash[:m.start()] + before_hash[m.end():]).strip()
    return amount, (reason or None), (category or None), (notes or None)


def _month_range(year: int, month: int):
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def _full_year(year: int) -> int:
    if year < 100:
        return 2000 + year if year < 50 else 1900 + year
    return year


def parse_date_input(date_input, today=None):
    """Parse a date or date range into (start, end) ISO strings, or (None, None).

    Supports dd-mm-yyyy, yyyy-mm-dd, mm-yyyy, mm-yy (with - or /), month names
    ("August 2025", "aug 25"), "today", "yesterday", "this week", "last week",
    "this month", "last month", and ranges joined with "to".
    """
    if not date_input:
        return None, None
    today = today or date.today()
    text = date_input.strip().lower()

    if text == "today":
        return today.isoformat(), today.isoformat()
    if text == "yesterday":
        d = today - timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if text == "this week":
        monday = today - timedelta(days=today.weekday())
        return monday.isoformat(), today.isoformat()
    if text == "last week":
        # Monday to Sunday of the previous calendar week
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(days=7)
        return start.isoformat(), (this_monday - timedelta(days=1)).isoformat()
    if text == "this month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if text == "last month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return _month_range(last_prev.year, last_prev.month)

    if " to " in text:
        left, right = text.split(" to ", 1)
        start, _ = parse_date_input(left, today)
        _, end = parse_date_input(right, today)
        if start and end and start <= end:
            return start, end
        return None, None

    text = text.replace("/", "-")
    try:
        for name, num in MONTHS.items():
            if text.startswith(name + " ") or text == name:
                year_part = text[len(name):].strip()
                if year_part.isdigit():
                    return _month_range(_full_year(int(year_part)), num)
                return None, None

        parts = text.split("-")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            p1, p2, p3 = parts
            if len(p3) == 4 and len(p1) <= 2:
                d = date(int(p3), int(p2), int(p1))
            elif len(p1) == 4:
                d = date(int(p1), int(p2), int(p3))
            else:
                return None, None
            return d.isoformat(), d.isoformat()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            return _month_range(_full_year(int(parts[1])), int(parts[0]))
    except ValueError:
        pass
    return None, None


def rows_from_values(values):
    """Turn ws.get_all_values() output (header first) into entry dicts with sheet row numbers."""
    rows = []
    for index, raw in enumerate(values[1:], start=2):
        if not any(cell.strip() for cell in raw):
            continue
        padded = (list(raw) + [""] * len(COLUMNS))[:len(COLUMNS)]
        entry = dict(zip(COLUMNS, padded))
        entry["row"] = index
        rows.append(entry)
    return rows


def to_float(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def filter_entries_by_criteria(entries, criteria):
    """Filter entry dicts by date range, category, amount (eq/gt/lt) and description."""
    result = []
    for entry in entries:
        entry_date = (entry.get("ts") or "").split(" ")[0]
        if "date_start" in criteria and "date_end" in criteria:
            if not (criteria["date_start"] <= entry_date <= criteria["date_end"]):
                continue
        if "category" in criteria:
            category = (entry.get("category") or "uncategorized").lower()
            if criteria["category"].lower() not in category:
                continue
        if "amount" in criteria:
            value, target = to_float(entry.get("amount")), to_float(criteria["amount"])
            if value is None or target is None:
                continue
            op = criteria.get("amount_op", "eq")
            if (op == "gt" and not value > target) or (op == "lt" and not value < target) \
                    or (op == "eq" and value != target):
                continue
        if "reason" in criteria:
            if criteria["reason"].lower() not in (entry.get("reason") or "").lower():
                continue
        result.append(entry)
    return result


def summarize(entries):
    """Return (total, [(category, amount, count), ...] sorted by amount desc)."""
    totals = defaultdict(float)
    counts = defaultdict(int)
    for entry in entries:
        value = to_float(entry.get("amount"))
        if value is None:
            continue
        category = (entry.get("category") or "uncategorized").strip() or "uncategorized"
        totals[category] += value
        counts[category] += 1
    by_category = sorted(((c, totals[c], counts[c]) for c in totals), key=lambda t: -t[1])
    return sum(totals.values()), by_category


def md(text) -> str:
    """Escape user-supplied text for Telegram's legacy Markdown parse mode."""
    if text is None:
        return ""
    return re.sub(r'([_*`\[])', r'\\\1', str(text))


def format_amount(value: float) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def now_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
