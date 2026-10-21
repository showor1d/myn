"""Personal spending ledger, with original amounts and immutable reference-rate conversions."""
import sqlite3
import json
import uuid
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import usage_stats

TOKYO = timezone(timedelta(hours=9))
PERSONAL_CATEGORIES = ("eat", "entertainment", "shop", "study", "transportation", "house", "utilities", "health")
CATEGORIES = PERSONAL_CATEGORIES + ("token",)
PERIOD_LABELS = {"day": "D", "week": "W", "month": "M", "year": "Y"}
CHART_COLORS = ("#9744ec", "#ec8baf", "#57b8bb", "#b594f0", "#739bd4",
                "#c9a66d", "#77ac87", "#d47977", "#6bc6df")
CURRENCIES = {"CNY": ("¥", 100), "JPY": ("¥", 1), "USD": ("$", 100)}
FILE = "expenses.sqlite3"


def money(minor, currency):
    symbol, factor = CURRENCIES[currency]
    return "%s%s %s" % (symbol, format(Decimal(minor) / factor, ",.0f" if factor == 1 else ",.2f"), currency)


def rate_note(rows, config, start, end, now):
    """One latest quote per Tokyo day, then an equal-day mean in 100 CNY units.

    Cached official quotes take precedence over immutable transaction snapshots.
    Missing cache days can use the latest dated ledger snapshot; never fabricate
    missing days or weight rates by the number/amount of purchases.
    """
    import expense_rates
    cached = expense_rates.cached_quotes(config)
    fallback = {}
    for row in rows:
        if not row.get("fx_rates"):
            continue
        when = datetime.fromisoformat(row["occurred_at"]).astimezone(TOKYO)
        day = when.date().isoformat()
        if day not in fallback or when > fallback[day][0]:
            fallback[day] = (when, row["fx_rates"])
    daily = []
    day = start.date()
    last = min((end - timedelta(microseconds=1)).date(), now.date())
    while day <= last:
        key = day.isoformat()
        try:
            rates = expense_rates.select_quote(cached, key)["rates"]
        except (ValueError, KeyError, InvalidOperation):
            rates = json.loads(fallback[key][1]) if key in fallback else None
        if rates:
            try:
                values = {code: Decimal(rates[code]) for code in ("USD", "CNY", "JPY")}
                if all(value.is_finite() and value > 0 for value in values.values()):
                    daily.append(tuple(100 * values[code] / values["CNY"] for code in ("USD", "JPY")))
            except (ValueError, KeyError, InvalidOperation):
                pass
        day += timedelta(days=1)
    if not daily:
        return ""
    return "%s USD = 100 CNY = %s JPY" % tuple(
        format(sum(values[i] for values in daily) / len(daily), ".4f").rstrip("0").rstrip(".")
        for i in range(2))


def records(config):
    path = Path(config) / FILE
    if not path.exists():
        return []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM expenses ORDER BY occurred_at DESC, id")]


def add(config, amount, currency, category, description, occurred_at, source_key, rate_provider=None):
    if currency not in CURRENCIES or category not in PERSONAL_CATEGORIES:
        raise ValueError('Invalid currency or category')
    if not isinstance(description, str) or not description.strip() or len(description) > 300:
        raise ValueError('A brief expense description is required')
    if not isinstance(source_key, str) or not source_key or len(source_key) > 500:
        raise ValueError('A source for this expense is required')
    try:
        value = Decimal(str(amount)) * CURRENCIES[currency][1]
        if not value.is_finite() or value <= 0 or value != value.to_integral_value() or value > 10**12:
            raise ValueError("Amount must be positive and use the currency's precision")
        when = datetime.fromisoformat(occurred_at)
        if when.tzinfo is None:
            when = when.replace(tzinfo=TOKYO)
    except (InvalidOperation, TypeError):
        raise ValueError('Invalid amount or expense date')
    item = dict(id=uuid.uuid4().hex, amount_minor=int(value), currency=currency, category=category,
                description=description.strip(), occurred_at=when.astimezone(TOKYO).isoformat(),
                source_key=source_key)
    # Check retries before requesting rates so a later quote cannot alter an already saved expense.
    old = next((r for r in records(config) if r["source_key"] == source_key), None)
    if old:
        if any(old[k] != item[k] for k in item if k != "id"):
            raise ValueError('This source already has a different record; check it first')
        return old
    import expense_rates
    quote = (rate_provider or expense_rates.quote)(config, when.astimezone(TOKYO).date().isoformat())
    original = Decimal(item["amount_minor"]) / CURRENCIES[currency][1]
    for code in ("USD", "JPY", "CNY"):
        converted = original * Decimal(quote["rates"][code]) / Decimal(quote["rates"][currency])
        item[code.lower()] = int((converted * CURRENCIES[code][1]).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    item["fx_date"], item["fx_source"], item["fx_rates"] = quote["date"], quote["source"], json.dumps(quote["rates"])
    path = Path(config) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS expenses (
            id TEXT PRIMARY KEY, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
            category TEXT NOT NULL, description TEXT NOT NULL, occurred_at TEXT NOT NULL,
            source_key TEXT UNIQUE NOT NULL, usd INTEGER NOT NULL, jpy INTEGER NOT NULL, cny INTEGER NOT NULL,
            fx_date TEXT NOT NULL, fx_source TEXT NOT NULL, fx_rates TEXT NOT NULL)""")
        db.row_factory = sqlite3.Row
        old = db.execute("SELECT * FROM expenses WHERE source_key=?", (source_key,)).fetchone()
        if old:
            old = dict(old)
            if any(old[k] != item[k] for k in ("amount_minor", "currency", "category", "description", "occurred_at")):
                raise ValueError('This source already has a different record; check it first. No duplicate was saved')
            return old
        db.execute("""INSERT INTO expenses VALUES
            (:id,:amount_minor,:currency,:category,:description,:occurred_at,:source_key,
             :usd,:jpy,:cny,:fx_date,:fx_source,:fx_rates)""", item)
    return item


def token_rows(config, calls):
    """Project the existing ledger, without copying/reinserting any billed calls."""
    import expense_rates
    rates = expense_rates.cached_quotes(config)
    groups = {}
    for call in calls:
        day = call.when.date().isoformat()
        key = (day, call.group)
        row = groups.setdefault(key, dict(
            category="token", description=call.group, occurred_at=call.when.isoformat(),
            currency="USD", usd=Decimal(0), tokens=0, calls=0, unpriced=False, estimated=False))
        row["usd"] += Decimal(str(call.cost)) * 100
        row["tokens"] += call.tokens
        row["calls"] += 1
        row["unpriced"] |= not call.priced
        row["estimated"] |= call.estimated
    for (day, _), row in groups.items():
        row["amount_minor"] = row["usd"]
        try:
            quote = expense_rates.select_quote(rates, day)
            row["fx_rates"] = json.dumps(quote["rates"])
            for code in ("JPY", "CNY"):
                row[code.lower()] = (row["usd"] / 100 * Decimal(quote["rates"][code]) /
                                    Decimal(quote["rates"]["USD"]) * CURRENCIES[code][1])
        except (ValueError, KeyError, InvalidOperation):
            row["jpy"] = row["cny"] = None if row["usd"] else 0
        row["detail"] = '%s · %s tokens · %s calls%s' % (
            day[5:], format(row["tokens"], ","), row["calls"],
            ' · Includes unpriced records' if row["unpriced"] else "")
    return list(groups.values())


def _trend(rows, period, start, category):
    """Fixed calendar axes; monthly/yearly charts provide a wider time overview."""
    dated = [(datetime.fromisoformat(r["occurred_at"]).astimezone(TOKYO), r) for r in rows]
    buckets = []
    if period == "day":
        buckets = [(start+timedelta(hours=h), start+timedelta(hours=h+1), "%02d:00" % h)
                   for h in range(24)]
        positions = [(h+.5)/24 for h in range(24)]
        ticks = [[h/24, '%02d:00' % h] for h in (0, 6, 12, 18, 24)]
    elif period == "week":
        buckets = [(start+timedelta(days=d), start+timedelta(days=d+1), label)
                   for d, label in enumerate(("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"))]
    elif period == "month":
        year_start = start.replace(month=1, day=1)
        buckets = [(year_start.replace(month=m),
                    year_start.replace(year=start.year+1) if m == 12 else year_start.replace(month=m+1),
                    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[m-1]) for m in range(1, 13)]
    else:
        years = sorted({when.year for when, _ in dated})
        buckets = [(start.replace(year=y, month=1, day=1), start.replace(year=y+1, month=1, day=1),
                    str(y)) for y in years]
    if period != "day":
        positions = [i/(len(buckets)-1) if len(buckets) > 1 else .5 for i in range(len(buckets))]
        if period == "year":
            positions = [(i+.5)/len(buckets) for i in range(len(buckets))]
        # Single-letter month captions repeat; retain unique names for animation
        # identity and hover details so Jan/Jun/Jul never overwrite one another.
        ticks = [[x, bucket[2][0] if period == "month" else bucket[2]]
                 for x, bucket in zip(positions, buckets)]
    values = [float(sum(r["usd"] for when, r in dated if a <= when < b and
                        (category is None or r["category"] == category))/100)
              for a, b, _ in buckets]
    return values, [label for _, _, label in buckets], positions, ticks


def receipt_rows(chosen, period):
    """Aggregate only the monthly/yearly presentation, preserving ledger entries."""
    if period not in ("month", "year"):
        return [{"name": r["description"], "cost": money(r["usd"], "USD"),
                 "detail": r.get("detail") or datetime.fromisoformat(r["occurred_at"]).strftime("%m-%d") +
                           " · " + r["category"] + " · " + money(r["amount_minor"], r["currency"])}
                for r in sorted(chosen, key=lambda row: row["usd"], reverse=True)]
    groups = {}
    for r in chosen:
        key = (r["category"], r["description"])
        group = groups.setdefault(key, dict(usd=0, amounts={}, count=0, tokens=0, calls=0, unpriced=False))
        group["usd"] += r["usd"]
        group["count"] += 1
        currency = r["currency"]
        group["amounts"][currency] = group["amounts"].get(currency, 0) + r["amount_minor"]
        group["tokens"] += r.get("tokens", 0)
        group["calls"] += r.get("calls", 0)
        group["unpriced"] |= r.get("unpriced", False)
    rows = []
    for (category, name), group in sorted(groups.items(), key=lambda item: -item[1]["usd"]):
        if category == "token":
            detail = 'tokens · %s tokens · %s calls%s' % (
                format(group["tokens"], ","), group["calls"],
                ' · Includes unpriced records' if group["unpriced"] else "")
        else:
            originals = " + ".join(money(group["amounts"][code], code)
                                   for code in ("USD", "CNY", "JPY") if code in group["amounts"])
            detail = '%s · %s entries · %s' % (category, group["count"], originals)
        rows.append({"name": name, "cost": money(group["usd"], "USD"), "detail": detail})
    return rows


def receipt_date(period, offset, start, end):
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    def day(value):
        return value.strftime("%Y/%m/%d") + " " + weekdays[value.weekday()]
    if period == "day":
        prefix = {0: "Today", -1: "Yesterday"}.get(offset, "")
        return (prefix + "  " if prefix else "") + day(start)
    if period == "week":
        prefix = {0: "This Week", -1: "Last Week"}.get(offset, "Week")
        return prefix + "  " + day(start) + " – " + day(end-timedelta(days=1))
    if period == "month":
        prefix = {0: "This Month", -1: "Last Month"}.get(offset, "Month")
        return prefix + "  " + start.strftime("%Y/%m")
    prefix = {0: "This Year", -1: "Last Year"}.get(offset, "Year")
    return prefix + "  " + start.strftime("%Y")


def report(config, period="day", offset=0, category=None, now=None):
    if period not in usage_stats.PERIODS or category not in (None, *CATEGORIES):
        raise ValueError('Invalid statistics range')
    now = (now or datetime.now(TOKYO)).astimezone(TOKYO)
    offset = max(-1000, min(0, int(offset)))
    start, end = usage_stats.bounds(period, offset, now)
    if period == "week":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=(now.weekday()+1) % 7)
        start += timedelta(weeks=offset)
        end = start + timedelta(days=7)
    all_rows = records(config)
    calls = usage_stats.load_calls(config, tz=TOKYO)
    all_rows += token_rows(config, calls)
    inside = [r for r in all_rows if start <= datetime.fromisoformat(r["occurred_at"]) < end]
    totals = {c: sum(r["usd"] for r in inside if r["category"] == c) for c in CATEGORIES}
    total = sum(totals.values())
    chosen = [r for r in inside if category is None or r["category"] == category]
    selected = sum(r["usd"] for r in chosen)
    if category is None:
        pie_slices = [{"name": c, "value": float(totals[c]/100),
                       "color": CHART_COLORS[CATEGORIES.index(c)]}
                      for c in sorted(CATEGORIES, key=lambda name: -totals[name]) if totals[c] > 0]
    else:
        # Match the receipt's item names; repeated purchases of an item add together.
        items = {}
        for row in chosen:
            name = row["description"]
            items[name] = items.get(name, 0) + row["usd"]
        pie_slices = [{"name": name, "value": float(value/100),
                       "color": CHART_COLORS[int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:8], 16)
                                             % len(CHART_COLORS)]}
                      for name, value in sorted(items.items(), key=lambda pair: -pair[1]) if value > 0]
    points, point_labels, positions, ticks = _trend(all_rows, period, start, category)
    date_label = receipt_date(period, offset, start, end)
    import receipt_art
    return {
        "t": "usage_receipt", "kind": "expenses", "currency": "USD",
        "zero_art": receipt_art.data_uri() if not selected else "",
        "print_key": ":".join((period, start.isoformat(), category or "all")),
        "date": date_label,
        "heading": category or 'Expense receipt', "selected_category": category,
        "metric_label": {"day": "DAILY EXPENSES", "week": "WEEKLY EXPENSES",
                         "month": "MONTHLY EXPENSES", "year": "YEARLY EXPENSES"}[period],
        "cost": money(selected, "USD"), "zero": not selected and not any(r.get("unpriced") for r in chosen),
        "categories": [{"name": c, "share": "%.1f%%" % (totals[c] * 100 / total if total else 0),
                        "percent": float(totals[c] * 100 / total) if total else 0,
                        "cost": money(totals[c], "USD")}
                       for c in sorted(CATEGORIES, key=lambda name: -totals[name])],
        "rows": receipt_rows(chosen, period),
        "series": {"cost_usd": [float(value) for value in points]},  # chart only; totals retain Decimal precision
        "series_labels": point_labels, "at_present": offset == 0,
        "series_positions": positions, "trend_ticks": ticks,
        "chart_kind": "pie" if period == "day" else "bar" if period == "year" else "line",
        "chart_slices": pie_slices,
        "has_earlier": any(datetime.fromisoformat(r["occurred_at"]) < start for r in all_rows) or any(c.when < start for c in calls),
        "totals": [[code, 'Awaiting conversion' if any(r[code.lower()] is None for r in chosen) else
                    money(sum(r[code.lower()] for r in chosen), code)] for code in ("USD", "CNY", "JPY")],
        "count": len(chosen), "note": rate_note(inside, config, start, end, now),
        "token_total": sum(r.get("tokens", 0) for r in chosen),
        "unpriced": 'Includes historical usage without pricing; the total includes only priced amounts' if any(r.get("unpriced") for r in chosen) else "",
        "change": "", "comparison": "", "trend": "",
    }
