"""Simulated-time helpers. All internal times are integer minutes from D0 00:00 (IST)."""
from __future__ import annotations

import datetime as dt

MIN_PER_DAY = 1440


class Clock:
    """Maps simulator timestamps to minutes-from-D0. D0 is learned from `/sim/state`, never assumed."""

    def __init__(self, d0_date: dt.date):
        self.d0 = d0_date
        self._day_cache: dict[str, int] = {}

    @classmethod
    def from_state(cls, state: dict) -> "Clock":
        t = dt.datetime.fromisoformat(state["ts"])
        d0 = (t - dt.timedelta(minutes=state["sim_minute"])).date()
        return cls(d0)

    def day_of(self, ts: str) -> int:
        d = self._day_cache.get(ts[:10])
        if d is None:
            d = (dt.date.fromisoformat(ts[:10]) - self.d0).days
            self._day_cache[ts[:10]] = d
        return d

    def minute_of(self, ts: str) -> int:
        return self.day_of(ts) * MIN_PER_DAY + int(ts[11:13]) * 60 + int(ts[14:16])


def fmt_minute(m: int) -> str:
    """Human label used throughout the audit trail: 'D3 14:00' (negative days are history)."""
    d, r = divmod(m, MIN_PER_DAY)
    return f"D{d} {r // 60:02d}:{r % 60:02d}"


def parse_label(s: str) -> int:
    """'D3 14:00' -> minutes. Also accepts a bare integer."""
    s = s.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    day, hm = s.split()
    h, m = hm.split(":")
    return int(day.lstrip("Dd")) * MIN_PER_DAY + int(h) * 60 + int(m)


def hour_of(m: int) -> int:
    return (m % MIN_PER_DAY) // 60


def day_of(m: int) -> int:
    return m // MIN_PER_DAY
