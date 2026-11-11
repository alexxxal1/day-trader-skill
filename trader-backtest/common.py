"""Offline helpers. No broker imports, writes to strategy modules or live clocks."""
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EPOCH = dt.datetime(1970, 1, 1)


def module(folder, name):
    spec = importlib.util.spec_from_file_location("backtest_" + name, str(ROOT / folder / (name + ".py")))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def stamp(value):
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise ValueError("precise timestamp with timezone required")
    return value.astimezone(dt.timezone.utc).replace(tzinfo=None)


def iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(value):
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read(path):
    def invalid(value):
        raise ValueError("nonfinite JSON: " + value)
    return json.loads(Path(path).read_text(), parse_constant=invalid)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical(value))


class Clock:
    """A supplied exchange calendar, with the existing scanner's timezone rules.

    The calendar includes historical exceptional closures. The existing strategy
    functions receive this read-only dependency instead of a wall clock.
    """
    def __init__(self, sessions):
        self.sessions = sessions
        self.scan = module("trader-scanner", "scan")
        self.mini = module("trader-scanner", "minervini")

    def session(self, day):
        value = self.sessions.get(day.isoformat())
        return tuple(dt.time.fromisoformat(t) for t in value) if value else None

    def to_eastern(self, value):
        return self.scan.to_eastern(value)

    def eastern_to_utc(self, value):
        return self.scan.eastern_to_utc(value)

    def ts_to_eastern(self, value):
        return self.to_eastern(EPOCH + dt.timedelta(seconds=value))

    def minervini_module(self):
        return self.mini

    def close(self, day):
        day = dt.date.fromisoformat(day) if isinstance(day, str) else day
        return self.eastern_to_utc(dt.datetime.combine(day, self.session(day)[1]))


def calendar_2024_2027():
    """NYSE published regular/early session schedule, pinned; no weekday fallback."""
    closed = set("""2024-01-01 2024-01-15 2024-02-19 2024-03-29 2024-05-27
        2024-06-19 2024-07-04 2024-09-02 2024-11-28 2024-12-25
        2025-01-01 2025-01-09 2025-01-20 2025-02-17 2025-04-18 2025-05-26
        2025-06-19 2025-07-04 2025-09-01 2025-11-27 2025-12-25
        2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19
        2026-07-03 2026-09-07 2026-11-26 2026-12-25
        2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18
        2027-07-05 2027-09-06 2027-11-25 2027-12-24""".split())
    early = set("""2024-07-03 2024-11-29 2024-12-24 2025-07-03 2025-11-28
        2025-12-24 2026-11-27 2026-12-24 2027-11-26""".split())
    out, day = {}, dt.date(2024, 1, 1)
    while day.year < 2028:
        key = day.isoformat()
        if day.weekday() < 5 and key not in closed:
            out[key] = ["09:30", "13:00" if key in early else "16:00"]
        day += dt.timedelta(days=1)
    return out
