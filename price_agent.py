#!/usr/bin/env python3
"""Daily Estonian (EE) day-ahead electricity price agent.

Fetches tomorrow's hourly EE prices from Nord Pool (falls back to Elering),
builds a short summary, flags large price swings and pushes the result to a
phone via ntfy (https://ntfy.sh).

Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TALLINN = ZoneInfo("Europe/Tallinn")
AREA = "EE"

NORDPOOL_URL = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPriceIndices"
ELERING_URL = "https://dashboard.elering.ee/api/nps/price"
USER_AGENT = "Elektriagent/1.0 (+https://github.com/neogto-jpg/Elektriagent)"


@dataclass
class Config:
    high_price: float = 200.0  # €/MWh: alert when any hour is above this
    low_price: float = 0.0  # €/MWh: alert when any hour is at or below this
    spread: float = 150.0  # €/MWh: alert when max - min exceeds this
    cheap_block_hours: int = 3  # length of the "cheapest block" in the summary
    show_hourly: bool = True
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        def env(name: str) -> str | None:
            value = os.environ.get(name, "").strip()
            return value or None  # empty GitHub variables count as unset

        cfg = cls()
        for name, attr, cast in [
            ("HIGH_PRICE", "high_price", float),
            ("LOW_PRICE", "low_price", float),
            ("SPREAD_PRICE", "spread", float),
            ("CHEAP_BLOCK_HOURS", "cheap_block_hours", int),
            ("NTFY_SERVER", "ntfy_server", str),
            ("NTFY_TOPIC", "ntfy_topic", str),
            ("NTFY_TOKEN", "ntfy_token", str),
        ]:
            if (value := env(name)) is not None:
                setattr(cfg, attr, cast(value))
        if (value := env("SHOW_HOURLY")) is not None:
            cfg.show_hourly = value.lower() not in ("0", "false", "no", "off")
        return cfg


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def http_get_json(url: str, params: dict[str, str]) -> dict | None:
    """GET JSON; returns None when the server has no content yet (HTTP 204)."""
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        full_url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        if resp.status == 204 or not body.strip():
            return None
        return json.loads(body)


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC start/end of a calendar day in Estonia (handles DST: 23/24/25 h)."""
    start = datetime(day.year, day.month, day.day, tzinfo=TALLINN)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=TALLINN)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def expected_hours(day: date) -> int:
    start, end = local_day_bounds(day)
    return int((end - start).total_seconds() // 3600)


def parse_iso_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_nordpool(data: dict) -> dict[datetime, float]:
    """Nord Pool data portal response -> {hour start (UTC): €/MWh}."""
    prices: dict[datetime, float] = {}
    for entry in data.get("multiIndexEntries") or []:
        value = (entry.get("entryPerArea") or {}).get(AREA)
        if value is None:
            continue
        prices[parse_iso_utc(entry["deliveryStart"])] = float(value)
    return prices


def parse_elering(data: dict) -> dict[datetime, float]:
    """Elering NPS response -> hourly averages {hour start (UTC): €/MWh}.

    Elering returns 15-minute prices since the move to 15-min MTU, so the
    values are averaged per hour to match the Nord Pool 60-min index.
    """
    buckets: dict[datetime, list[float]] = {}
    for entry in (data.get("data") or {}).get(AREA.lower()) or []:
        if entry.get("price") is None:
            continue
        ts = datetime.fromtimestamp(int(entry["timestamp"]), tz=timezone.utc)
        hour = ts.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(float(entry["price"]))
    return {hour: sum(v) / len(v) for hour, v in buckets.items()}


def filter_local_day(prices: dict[datetime, float], day: date) -> dict[datetime, float]:
    start, end = local_day_bounds(day)
    return {ts: p for ts, p in sorted(prices.items()) if start <= ts < end}


def fetch_nordpool(day: date) -> dict[datetime, float]:
    # Nord Pool delivery days are CET days. The Estonian day starts one hour
    # earlier, so its first hour lives in the previous CET delivery day.
    prices: dict[datetime, float] = {}
    for delivery_date in (day - timedelta(days=1), day):
        data = http_get_json(
            NORDPOOL_URL,
            {
                "date": delivery_date.isoformat(),
                "market": "DayAhead",
                "indexNames": AREA,
                "currency": "EUR",
                "resolutionInMinutes": "60",
            },
        )
        if data:
            prices.update(parse_nordpool(data))
    return filter_local_day(prices, day)


def fetch_elering(day: date) -> dict[datetime, float]:
    start, end = local_day_bounds(day)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    data = http_get_json(
        ELERING_URL,
        {"start": start.strftime(fmt), "end": (end - timedelta(seconds=1)).strftime(fmt)},
    )
    return filter_local_day(parse_elering(data or {}), day)


def get_prices(day: date) -> tuple[dict[datetime, float], str] | None:
    """Try each source; return the first complete day of prices."""
    needed = expected_hours(day)
    for name, fetch in (("Nord Pool", fetch_nordpool), ("Elering", fetch_elering)):
        try:
            prices = fetch(day)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            print(f"{name}: fetch failed: {exc}", file=sys.stderr)
            continue
        if len(prices) >= needed:
            return prices, name
        print(f"{name}: {len(prices)}/{needed} hours available", file=sys.stderr)
    return None


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


@dataclass
class Hour:
    start: datetime  # local Tallinn time
    price: float  # €/MWh

    @property
    def label(self) -> str:
        return self.start.strftime("%H:%M")


def to_hours(prices: dict[datetime, float]) -> list[Hour]:
    return [Hour(ts.astimezone(TALLINN), p) for ts, p in sorted(prices.items())]


def ranges(hours: list[Hour], predicate) -> list[list[Hour]]:
    """Group consecutive hours matching predicate."""
    groups: list[list[Hour]] = []
    for h in hours:
        if predicate(h):
            if groups and groups[-1][-1].start + timedelta(hours=1) == h.start:
                groups[-1].append(h)
            else:
                groups.append([h])
    return groups


def fmt_range(group: list[Hour]) -> str:
    end = group[-1].start + timedelta(hours=1)
    end_label = "24:00" if end.hour == 0 else end.strftime("%H:%M")
    return f"{group[0].label}–{end_label}"


def cheapest_block(hours: list[Hour], length: int) -> tuple[list[Hour], float] | None:
    if length < 1 or len(hours) < length:
        return None
    best = min(
        (hours[i : i + length] for i in range(len(hours) - length + 1)),
        key=lambda block: sum(h.price for h in block),
    )
    return best, sum(h.price for h in best) / length


def find_alerts(hours: list[Hour], cfg: Config) -> list[str]:
    alerts: list[str] = []

    expensive = ranges(hours, lambda h: h.price > cfg.high_price)
    if expensive:
        peak = max(hours, key=lambda h: h.price)
        spans = ", ".join(fmt_range(g) for g in expensive)
        alerts.append(
            f"⚠️ Expensive (>{cfg.high_price:g} €/MWh): {spans}; "
            f"peak {peak.price:.1f} at {peak.label}"
        )

    cheap = ranges(hours, lambda h: h.price <= cfg.low_price)
    if cheap:
        low = min(hours, key=lambda h: h.price)
        spans = ", ".join(fmt_range(g) for g in cheap)
        alerts.append(
            f"🟢 Very cheap (≤{cfg.low_price:g} €/MWh): {spans}; "
            f"lowest {low.price:.1f} at {low.label}"
        )

    lo = min(hours, key=lambda h: h.price)
    hi = max(hours, key=lambda h: h.price)
    if hi.price - lo.price > cfg.spread:
        alerts.append(
            f"↕️ Big swing: {hi.price - lo.price:.1f} €/MWh between "
            f"{lo.label} ({lo.price:.1f}) and {hi.label} ({hi.price:.1f})"
        )
    return alerts


def build_message(day: date, hours: list[Hour], source: str, cfg: Config) -> dict:
    """Return the ntfy payload (without topic)."""
    avg = sum(h.price for h in hours) / len(hours)
    lo = min(hours, key=lambda h: h.price)
    hi = max(hours, key=lambda h: h.price)
    alerts = find_alerts(hours, cfg)

    lines: list[str] = []
    if alerts:
        lines += alerts + [""]
    lines += [
        f"Avg {avg:.1f} €/MWh ({avg / 10:.1f} c/kWh)",
        f"Min {lo.price:.1f} at {lo.label} · Max {hi.price:.1f} at {hi.label}",
    ]
    block = cheapest_block(hours, cfg.cheap_block_hours)
    if block:
        group, block_avg = block
        lines.append(f"Cheapest {cfg.cheap_block_hours}h: {fmt_range(group)} (avg {block_avg:.1f})")

    if cfg.show_hourly:
        lines += ["", "Hourly €/MWh:"]
        cells = [f"{h.start:%H} {h.price:.0f}" for h in hours]
        lines += [" | ".join(cells[i : i + 4]) for i in range(0, len(cells), 4)]

    lines += ["", f"Excl. VAT & fees · source: {source}"]

    title = f"EE power {day:%a %d.%m}: avg {avg / 10:.1f} c/kWh"
    if alerts:
        title = "⚡ Price alert · " + title
    return {
        "title": title,
        "message": "\n".join(lines),
        "priority": 4 if alerts else 3,
        "tags": ["warning"] if alerts else ["zap"],
    }


# --------------------------------------------------------------------------
# Notification
# --------------------------------------------------------------------------


def send_ntfy(payload: dict, cfg: Config) -> None:
    body = json.dumps({"topic": cfg.ntfy_topic, **payload}).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    req = urllib.request.Request(cfg.ntfy_server.rstrip("/"), data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def deliver(payload: dict, cfg: Config, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry run] {payload['title']} (priority {payload['priority']})")
        print(payload["message"])
        return
    send_ntfy(payload, cfg)
    print(f"Sent: {payload['title']}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=date.fromisoformat, help="delivery day (default: tomorrow, Tallinn time)")
    parser.add_argument("--dry-run", action="store_true", help="print the message instead of sending it")
    parser.add_argument("--wait-minutes", type=int, default=90, help="keep retrying this long if prices are not out yet")
    parser.add_argument("--retry-interval", type=int, default=10, help="minutes between retries")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    if not args.dry_run and not cfg.ntfy_topic:
        print("NTFY_TOPIC is not set (use --dry-run to test without sending)", file=sys.stderr)
        return 2

    day = args.date or (datetime.now(TALLINN).date() + timedelta(days=1))
    deadline = time.monotonic() + args.wait_minutes * 60

    while (result := get_prices(day)) is None:
        if time.monotonic() + args.retry_interval * 60 > deadline:
            deliver(
                {
                    "title": f"EE power {day:%a %d.%m}: prices not available",
                    "message": f"Could not get day-ahead prices for {day} from Nord Pool or Elering. "
                    "Check the GitHub Actions log.",
                    "priority": 4,
                    "tags": ["x"],
                },
                cfg,
                args.dry_run,
            )
            return 1
        print(f"Prices for {day} not ready, retrying in {args.retry_interval} min", file=sys.stderr)
        time.sleep(args.retry_interval * 60)

    prices, source = result
    deliver(build_message(day, to_hours(prices), source, cfg), cfg, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
