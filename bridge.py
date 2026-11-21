#!/usr/bin/env python3
"""A bridge from a Nanit camera to Miara: sleeps as they happen, plus room temperature and humidity.

Unofficial. Nanit has no public API; this uses the calls its app makes. See README.md.

  ./install.sh                         set up, step by step (runs `python bridge.py setup`)
  python bridge.py status | stop       is it running in the background; stop it
  python bridge.py probe               show the shape of what Nanit returns (field names only)
  python bridge.py --dry-run           read Nanit, print what would be sent, send nothing
  python bridge.py                     one sync (for cron or launchd)
  python bridge.py watch               keep running: every minute or two, sooner when Miara asks
  python bridge.py --sandbox [watch]   made-up Nanit data into Miara's public sandbox family

Your Nanit login stays on this machine. This file never sends it to Miara.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import nanit

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    """Read KEY=value lines from .env into the environment, without replacing what's already set."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    load_env()

DEFAULT_BASE = "https://getmiara.com/api/v1"
SANDBOX_TOKEN = "miara_pat_5a4d0b0c5a4d0b0c5a4d0b0c5a4d0b0c5a4d0b0c"

# Prefix of every source_event_id this bridge writes.
SOURCE = "nanit"
DECLARED_METRICS: list[dict] = []

# How far back each sync looks. Nanit finishes a sleep's record after the fact, so every sync
# re-reads the last two days; rows that haven't changed since they were sent aren't sent again.
LOOKBACK = timedelta(hours=48)
# A wake shorter than this doesn't end a sleep (a stir, a resettle).
SHORT_WAKE = timedelta(minutes=5)
# Nanit's feed posts FELL_ASLEEP and WOKE_UP up to ~17 minutes late. A sleep the calendar has just
# closed stays open this long, unless the feed has its wake, in case it's a stir the feed hasn't caught.
FEED_LAG = timedelta(minutes=20)
# A sleep whose wake we never saw is treated as over after this long.
MAX_OPEN = timedelta(hours=16)
# Room readings at most this often.
ROOM_EVERY = timedelta(minutes=10)
# Also send sleeps someone logged in the Nanit app. Off: if you log in Miara too, they'd overlap.
# Add what people log by hand in the Nanit app (diapers, bottles, nursing, pumping, naps). Setup asks.
INCLUDE_NANIT_LOGS = os.environ.get("NANIT_INCLUDE_LOGS", "1") != "0"

# watch: how long to wait between syncs.
WATCH_ASLEEP = 60        # a sleep is open: catch the wake within a minute
WATCH_ACTIVE = 120       # something happened in the last two hours
WATCH_QUIET = 600        # nothing for two hours
WATCH_MAX_BACKOFF = 900  # after errors


# ---------------------------------------------------------------------------
# 1. fetch: what Nanit has for each baby in the last LOOKBACK
# ---------------------------------------------------------------------------

async def fetch_async(n: nanit.Nanit, state: dict, now: datetime) -> list[dict]:
    """Return items for map(): one {"type": "baby", ...} block per baby."""
    start, end = int((now - LOOKBACK).timestamp()), int(now.timestamp())
    out = []
    for baby in await n.babies():
        if not baby.uid:
            continue
        feed = await n.feed(baby.uid, start, end)
        calendar = await n.calendar(baby.uid, start, end)
        room = (None, None)
        last_room = state.get("room_at", {}).get(baby.uid)
        if last_room is None or now - parse_time(last_room) >= ROOM_EVERY - timedelta(seconds=5):
            room = await n.room(baby)
        out.append({"type": "baby", "uid": baby.uid, "feed": feed, "calendar": calendar,
                    "room": {"celsius": room[0], "humidity": room[1]}, "at": iso(now)})
    return out


# ---------------------------------------------------------------------------
# 2. map: Nanit's timeline into Miara sleeps and readings
# ---------------------------------------------------------------------------

STARTS = {"FELL_ASLEEP", "PUT_TO_SLEEP"}
ENDS = {"WOKE_UP", "REMOVED_ASLEEP", "REMOVED"}


def sleeps_from_feed(feed: list[dict], open_sleep: dict | None = None) -> list[dict]:
    """Nanit's events into sleeps: {"key", "start", "end" or None}.

    A sleep starts at FELL_ASLEEP, or PUT_TO_SLEEP (laid down already asleep). It ends at
    WOKE_UP, REMOVED_ASLEEP (lifted out still asleep), or REMOVED.

    A wake shorter than SHORT_WAKE joins the sleeps on either side. `open_sleep` is a sleep
    still open at the end of the last sync, for a wake whose start has scrolled out of the
    lookback window.
    """
    events = sorted((e for e in feed if e.get("kind", "event") == "event" and e.get("key") in STARTS | ENDS
                     and not e.get("deleted_at") and e.get("time") is not None),
                    key=lambda e: float(e["time"]))
    sleeps: list[dict] = []
    cur = dict(open_sleep) if open_sleep else None
    for e in events:
        at = datetime.fromtimestamp(float(e["time"]), timezone.utc)
        if e["key"] in STARTS:
            if cur and cur["end"] is None:
                continue  # already asleep
            if cur and cur["end"] is not None and at - cur["end"] < SHORT_WAKE:
                cur["end"] = None  # a short wake: the same sleep goes on
                continue
            if cur:
                sleeps.append(cur)
            cur = {"key": str(e.get("uid") or int(float(e["time"]))), "start": at, "end": None}
        else:  # WOKE_UP, REMOVED_ASLEEP, REMOVED
            if cur and cur["end"] is None and at > cur["start"]:
                cur["end"] = at
    if cur:
        sleeps.append(cur)
    return sleeps


def sleeps_from_calendar(calendar: list[dict]) -> list[dict]:
    """Finished sleeps from Nanit's calendar: auto_sleep (the camera saw it) and, if asked, sleep (logged in Nanit)."""
    kinds = {"auto_sleep"} | ({"sleep"} if INCLUDE_NANIT_LOGS else set())
    out = []
    for c in calendar:
        if c.get("type") not in kinds or c.get("time") is None or not c.get("duration"):
            continue
        start = datetime.fromtimestamp(float(c["time"]), timezone.utc)
        # `duration` is the one to trust: `net_duration` has come back doubled.
        out.append({"key": str(c.get("uid") or int(float(c["time"]))), "start": start,
                    "end": start + timedelta(seconds=float(c["duration"]))})
    # Nanit splits a night at every wake, even a minute long. Join pieces closer than
    # SHORT_WAKE, the same rule the live sleep follows, so the final entry matches it.
    joined: list[dict] = []
    for s in sorted(out, key=lambda s: s["start"]):
        if joined and s["start"] - joined[-1]["end"] < SHORT_WAKE:
            joined[-1]["end"] = max(joined[-1]["end"], s["end"])
        else:
            joined.append(dict(s))
    return joined


# ---------------------------------------------------------------------------
# Logged in the Nanit app: diapers, bottles, nursing, pumping
# ---------------------------------------------------------------------------

OZ_ML = 29.5735
LOG_KINDS = {"diaper_change": "diaper", "bottle_feed": "bottle", "bottle": "bottle", "nursing": "nursing",
             "breastfeeding": "nursing", "breast_feeding": "nursing", "pumping": "pump", "pump": "pump"}
NOT_FROM_BRIDGES = {"solids", "solid", "solid_food", "medication", "medicine", "temperature", "growth",
                    "weight", "height", "head_circumference"}


def _first(item: dict, *names: str):
    for n in names:
        v = item.get(n)
        if v not in (None, ""):
            return v
    return None


def _ml(item: dict, *amount_names: str) -> float | None:
    amount = _first(item, *amount_names)
    if amount is None:
        return None
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    unit = str(_first(item, "unit", "units", "amount_unit", "volume_unit") or "ml").lower()
    return round(amount * OZ_ML, 1) if unit.startswith("oz") or "ounce" in unit else round(amount, 1)


def _seconds(item: dict, *names: str) -> float | None:
    v = _first(item, *names)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def diaper_details(item: dict) -> dict | None:
    raw = str(_first(item, "change_type", "diaper_type", "kind_of_change") or "").lower()
    pee = any(w in raw for w in ("pee", "wet", "urine"))
    poo = any(w in raw for w in ("poo", "dirty", "stool", "bm"))
    if raw in ("mixed", "both") or (pee and poo):
        return {"diaper": "mixed"}
    if poo:
        return {"diaper": "dirty"}
    if pee:
        return {"diaper": "wet"}
    if raw == "dry":
        return {"diaper": "dry"}
    return None


def bottle_details(item: dict) -> dict | None:
    ml = _ml(item, "amount", "volume", "quantity", "amount_ml", "volume_ml")
    if not ml or not 0 < ml < 1000:
        return None
    what = str(_first(item, "content", "contents", "milk_type", "food_type", "bottle_type") or "").lower()
    contents = ("mixed" if "mix" in what else "formula" if "formula" in what
                else "breast_milk" if "breast" in what or "pumped" in what else None)
    if contents is None:
        return None
    return {"volume_ml": ml, "contents": contents}


def nursing_details(item: dict) -> dict | None:
    left = _seconds(item, "left_duration", "left_seconds", "left_time", "left")
    right = _seconds(item, "right_duration", "right_seconds", "right_time", "right")
    if left is None and right is None:
        total, side = _seconds(item, "duration"), str(item.get("side") or "").lower()
        if total is None or side not in ("left", "right"):
            return None
        left, right = (total, 0) if side == "left" else (0, total)
    return {"left_seconds": left or 0, "right_seconds": right or 0}


def pump_details(item: dict) -> dict | None:
    out = {}
    for key, names in (("total_ml", ("amount", "volume", "total_amount", "total")),
                       ("left_ml", ("left_amount", "left_volume")), ("right_ml", ("right_amount", "right_volume"))):
        ml = _ml(item, *names)
        if ml:
            out[key] = ml
    secs = _seconds(item, "duration", "duration_seconds")
    if secs:
        out["duration_seconds"] = secs
    return out or None


DETAILS = {"diaper": diaper_details, "bottle": bottle_details, "nursing": nursing_details, "pump": pump_details}


def logs_from_calendar(baby_uid: str, calendar: list[dict], child: str) -> tuple[list[dict], dict]:
    """Entries for what was logged in the Nanit app, and {type: count} of what couldn't be read."""
    entries, unread = [], {}
    for c in calendar:
        t = str(c.get("type") or "")
        if t in ("auto_sleep", "sleep") or c.get("time") is None or c.get("deleted_at"):
            continue  # sleeps are handled with the camera's
        kind = LOG_KINDS.get(t)
        details = DETAILS[kind](c) if kind else None
        if details is None:
            label = t + (" (Miara can't take these from a bridge yet)" if t in NOT_FROM_BRIDGES else "")
            unread[label] = unread.get(label, 0) + 1
            continue
        raw = {k: v for k, v in c.items() if k not in ("user", "comment", "baby_uid")}  # not who logged it; the note goes as a note
        entry = {"source_event_id": f"{SOURCE}:{baby_uid}:log:{c.get('uid') or int(float(c['time']))}",
                 "kind": kind, "child_id": child,
                 "started_at": iso(datetime.fromtimestamp(float(c["time"]), timezone.utc)),
                 "details": details, "raw": raw}
        note = c.get("comment").strip() if isinstance(c.get("comment"), str) else ""
        if note:
            entry["note"] = note[:2000]  # what someone typed in the Nanit app; left out when there's none
        entries.append(entry)
    return entries, unread


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    far = datetime.max.replace(tzinfo=timezone.utc)
    return a_start < (b_end or far) and b_start < (a_end or far)


def merge_sleeps(baby_uid: str, feed_sleeps: list[dict], cal_sleeps: list[dict], known: dict,
                 now: datetime | None = None) -> list[dict]:
    """One Miara entry per real sleep, whichever Nanit list it came from.

    A sleep keeps the source_event_id it was first sent with (from `known`: sid -> [start, end]),
    so the live entry made at FELL_ASLEEP is the one that gets its end and Nanit's final times.
    The calendar's finished sleep wins on times; the feed fills in a sleep still going on.

    Nanit's calendar writes each piece of a sleep as it closes, so a nap that stirred and went
    on shows there as finished while the feed has no wake yet. A sleep the feed says is still
    going stays open over the calendar's newest piece, and only the feed's wake (or a person in
    Miara) ends it. Over an older piece it doesn't: a later sleep in the calendar means Nanit
    missed the wake, and the calendar's end is the better guess.

    The feed is itself up to ~17 minutes late, so a piece the calendar closed less than FEED_LAG
    before `now` stays open too, unless the feed already has its wake. If the feed's FELL_ASLEEP
    and wake never come, the calendar's end stands once FEED_LAG has passed.
    """
    out: dict[str, dict] = {}

    def claim(s: dict) -> str:
        for sid, (ks, ke) in known.items():
            k_start = parse_time(ks)
            # A sleep we never saw end only reaches so far, or it would swallow every later one.
            k_end = parse_time(ke) if ke else k_start + MAX_OPEN
            if _overlaps(s["start"], s["end"], k_start, k_end):
                return sid
        for sid, o in out.items():
            if _overlaps(s["start"], s["end"], o["start"], o["end"]):
                return sid
        return event_id(f"{baby_uid}:{s['key']}")

    for s in cal_sleeps:
        sid = claim(s)
        if sid in out:  # Nanit split what we already have as one sleep: keep the whole span
            o = out[sid]
            o["start"], o["end"] = min(o["start"], s["start"]), max(o["end"], s["end"])
        else:
            out[sid] = {"start": s["start"], "end": s["end"], "from": "calendar"}
    newest_end = max((s["end"] for s in cal_sleeps), default=None)
    woke: set[str] = set()
    for s in feed_sleeps:
        sid = claim(s)
        if sid in out:
            o = out[sid]
            if s["end"] is not None:
                woke.add(sid)
            if s["end"] is None and o["end"] == newest_end:
                o["start"], o["end"], o["from"] = min(o["start"], s["start"]), None, "feed"
            elif s["end"] is not None and s["end"] - o["end"] > SHORT_WAKE:
                o["end"] = s["end"]  # the calendar hasn't written the rest of the sleep yet
            continue  # the calendar already has it, finished
        out[sid] = {"start": s["start"], "end": s["end"], "from": "feed"}
    if now is not None and newest_end is not None and now - newest_end < FEED_LAG:
        for sid, o in out.items():
            if o["from"] == "calendar" and o["end"] == newest_end and sid not in woke:
                o["end"], o["from"] = None, "held"  # maybe a stir: wait for the feed to catch up
    return [{"sid": sid, **v} for sid, v in out.items()]


def map(items: list[dict], state: dict | None = None, children: dict | None = None) -> tuple[list[dict], list[dict]]:  # noqa: A001
    """Turn fetch() output into (entries, readings), each with its Miara child_id.

    Updates `state` with the sleeps it made (so ids stay put) and when it last read the room.
    """
    state = state if state is not None else {}
    children = children or {}
    entries: list[dict] = []
    readings: list[dict] = []
    for it in items:
        if it.get("type") != "baby":
            continue
        uid = it["uid"]
        child = children.get(uid) or children.get("*")
        if not child:
            continue  # a Nanit baby you didn't connect to a Miara child
        known = state.setdefault("sleeps", {}).setdefault(uid, {})
        open_prev = state.setdefault("open", {}).get(uid)
        if open_prev and open_prev.get("held"):
            open_prev = None  # held only for the feed's lag: the feed never had it open
        if open_prev:
            open_prev = {"key": open_prev["key"], "start": parse_time(open_prev["start"]), "end": None}
        feed_sleeps = sleeps_from_feed(it.get("feed", []), open_prev)
        merged = merge_sleeps(uid, feed_sleeps, sleeps_from_calendar(it.get("calendar", [])), known, parse_time(it["at"]))
        still_open = None
        for s in merged:
            e = {"source_event_id": s["sid"], "kind": "sleep", "child_id": child,
                 "started_at": iso(s["start"]), "details": {}}
            if s["end"] is not None:
                e["ended_at"] = iso(s["end"])
            else:
                still_open = {"key": s["sid"], "start": iso(s["start"])}
                if s["from"] == "held":
                    still_open["held"] = True
            entries.append(e)
            known[s["sid"]] = [iso(s["start"]), iso(s["end"]) if s["end"] else None]
        if still_open:
            state["open"][uid] = still_open
        else:
            state["open"].pop(uid, None)

        if INCLUDE_NANIT_LOGS:
            logs, unread = logs_from_calendar(uid, it.get("calendar", []), child)
            entries.extend(logs)
            for label, count in unread.items():
                state.setdefault("unread_logs", {})[label] = count

        room = it.get("room") or {}
        minute = iso(parse_time(it["at"]).replace(second=0, microsecond=0))
        if room.get("celsius") is not None:
            readings.append({"metric": "room_temp_c", "child_id": child, "started_at": minute, "value": round(float(room["celsius"]), 1)})
        if room.get("humidity") is not None:
            readings.append({"metric": "humidity", "child_id": child, "started_at": minute, "value": round(float(room["humidity"]))})
        if room.get("celsius") is not None or room.get("humidity") is not None:
            state.setdefault("room_at", {})[uid] = it["at"]
    prune_known(state, LOOKBACK * 3)
    return entries, readings


def prune_known(state: dict, keep: timedelta, now: datetime | None = None) -> None:
    cutoff = (now or datetime.now(timezone.utc)) - keep
    for uid, known in state.get("sleeps", {}).items():
        for sid in [s for s, (start, _end) in known.items() if parse_time(start) < cutoff]:
            del known[sid]
    sent = state.get("sent", {})
    for key in [k for k, v in sent.items() if parse_time(v.get("at", "1970-01-01T00:00:00Z")) < cutoff]:
        del sent[key]
    kept = state.get("kept", {})
    for sid in [s for s, v in kept.items() if parse_time(v.get("started_at") or "1970-01-01T00:00:00Z") < cutoff]:
        del kept[sid]


def is_asleep(state: dict) -> bool:
    """A sleep is going on: Nanit has it open, and nobody has ended it in Miara."""
    return any(not ended_in_miara(state, o.get("key")) for o in state.get("open", {}).values())


def recently_active(state: dict, now: datetime, within: timedelta = timedelta(hours=2)) -> bool:
    for known in state.get("sleeps", {}).values():
        for start, end in known.values():
            if now - parse_time(end or start) < within:
                return True
    return False


def parse_children(spec: str) -> dict:
    """MIARA_CHILDREN: 'nanitBabyUid=miaraChildId,otherUid=otherId'. A lone id means every baby."""
    out = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
        else:
            out["*"] = part
    return out


# ---------------------------------------------------------------------------
# Made-up Nanit data, for --sandbox. Nothing real goes to the public sandbox.
# ---------------------------------------------------------------------------

class SampleNanit:
    """Answers like Nanit, with a made-up baby: a nap, last night, and one asleep right now."""

    def __init__(self, now: datetime | None = None):
        self.now = now or datetime.now(timezone.utc)

    def get(self, path: str, params: dict) -> dict:
        n = self.now.replace(second=0, microsecond=0)
        t = lambda h, m=0: (n - timedelta(hours=h, minutes=m)).timestamp()  # noqa: E731
        if path == "/babies":
            return {"babies": [{"uid": "samplebaby", "name": "Sample", "camera_uid": "samplecam"}]}
        if path.endswith("/events"):
            ev = lambda uid, key, at: {"uid": uid, "key": key, "time": at}  # noqa: E731
            return {"events": [ev("f1", "PUT_IN_BED", t(14, 5)), ev("f2", "FELL_ASLEEP", t(14)), ev("f3", "WOKE_UP", t(12, 10)),
                               ev("f4", "FELL_ASLEEP", t(12, 7)), ev("f5", "WOKE_UP", t(4)), ev("f6", "VISIT", t(3)),
                               ev("f7", "PUT_TO_SLEEP", t(0, 25))]}
        if path.endswith("/calendar"):
            return {"calendar": [{"uid": "c1", "type": "auto_sleep", "time": t(14), "duration": 10 * 3600},
                                 {"uid": "c2", "type": "diaper_change", "change_type": "pee", "time": t(3, 50)}]}
        return {}

    def room(self, baby) -> tuple[float, float]:
        return 21.4, 46.0


# ---------------------------------------------------------------------------
# Helpers for map()
# ---------------------------------------------------------------------------

def parse_time(value: str | datetime) -> datetime:
    """Any ISO 8601 string or datetime to an aware UTC datetime. Times without an offset are taken as UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(value: str | datetime) -> str:
    """The form Miara takes: 2026-11-13T07:00:00Z."""
    return parse_time(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_id(vendor_id: str) -> str:
    """A stable source_event_id from the service's own id: 'sample:sleep-20261116'.

    Allowed characters are letters, digits and . _ : - (up to 128). Anything else is
    replaced; if the result is too long it is shortened with a hash so it stays unique.
    """
    clean = re.sub(r"[^A-Za-z0-9._:-]", "_", str(vendor_id))
    out = f"{SOURCE}:{clean}"
    if len(out) > 128:
        digest = hashlib.sha256(str(vendor_id).encode()).hexdigest()[:16]
        out = f"{SOURCE}:{clean[:90]}.{digest}"
    return out


# ---------------------------------------------------------------------------
# 3. send: DONE. You shouldn't need to change anything below.
# ---------------------------------------------------------------------------

MAX_ROWS = 500
MAX_BODY_BYTES = 3_000_000  # the API takes 4 MB; stay under it
MAX_RAW_BYTES = 16 * 1024
MAX_TRIES = 5
WRITE_KINDS = {"sleep", "bottle", "nursing", "diaper", "pump"}
CORE_METRICS = {"breathing_rate", "heart_rate", "humidity", "room_temp_c", "sleep_stage", "sound_db", "spo2"}
METRIC_RE = re.compile(r"^([a-z][a-z0-9_]{0,31}|x\.[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31})$")
CHOICE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
STATUSES = ["saved", "updated", "unchanged", "suggested", "skipped", "rejected"]


class BridgeError(Exception):
    """Something to tell the person running the bridge, without a stack trace."""


class DailyLimit(BridgeError):
    pass


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def idempotency_key(path: str, body: dict) -> str:
    """The same batch always gets the same key, so a retry (or a re-run that fetches the same
    items) is recognised as the same request. A different batch gets a different key."""
    digest = hashlib.sha256((path + "\n" + canonical(body)).encode()).hexdigest()
    return f"miara-bridge-{digest[:48]}"


def batches(rows: list[dict], max_rows: int = MAX_ROWS, max_bytes: int = MAX_BODY_BYTES) -> list[list[dict]]:
    """Split rows into batches of at most max_rows and about max_bytes of JSON each."""
    out: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for row in rows:
        n = len(canonical(row).encode()) + 1
        if cur and (len(cur) >= max_rows or size + n > max_bytes):
            out.append(cur)
            cur, size = [], 0
        cur.append(row)
        size += n
    if cur:
        out.append(cur)
    return out


def check(entries: list[dict], readings: list[dict], declared: set[str]) -> tuple[list[dict], list[dict], list[tuple[str, str]]]:
    """Drop rows Miara would refuse anyway, with a reason each. Returns (entries, readings, problems)."""
    good_e, good_r, bad = [], [], []
    for e in entries:
        why = None
        if e.get("kind") not in WRITE_KINDS:
            why = f"kind {e.get('kind')!r} can't be sent (use {', '.join(sorted(WRITE_KINDS))})"
        elif not e.get("source_event_id"):
            why = "no source_event_id"
        elif not e.get("started_at"):
            why = "no started_at"
        elif len(canonical(e.get("raw", {})).encode()) > MAX_RAW_BYTES:
            why = "raw is over 16 KB"
        if why:
            bad.append((str(e.get("source_event_id")), why))
        else:
            good_e.append(e)
    for r in readings:
        why = None
        m = r.get("metric", "")
        v = r.get("value")
        if not METRIC_RE.match(m or ""):
            why = f"metric {m!r} isn't a valid name"
        elif m not in CORE_METRICS and m not in declared:
            why = f"{m} isn't a core metric and isn't in DECLARED_METRICS"
        elif isinstance(v, bool) or not isinstance(v, (int, float, str)):
            why = "value must be a number or one of the metric's choices"
        elif isinstance(v, str) and not CHOICE_RE.match(v):
            why = "text values must be one of a fixed set of lowercase choices, never free text"
        elif not r.get("started_at"):
            why = "no started_at"
        if why:
            bad.append((f"{m} {r.get('started_at')}", why))
        else:
            good_r.append(r)
    return good_e, good_r, bad


class Client:
    """The part that talks to Miara. `http` and `sleep` can be swapped out, which is how the tests run it."""

    def __init__(self, base: str, token: str, http=None, sleep=time.sleep):
        self.base = base.rstrip("/")
        self.token = token
        self.sleep = sleep
        self.replayed = 0  # requests Miara answered from an earlier identical one
        if http is None:
            import requests  # the one dependency
            http = requests.Session()
        self.http = http

    def request(self, method: str, path: str, body: dict | None = None, key: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "User-Agent": "miara-bridge/1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if method == "POST":
            headers["Idempotency-Key"] = key or idempotency_key(path, body or {})
        data = canonical(body).encode() if body is not None else None
        last = ""
        for attempt in range(MAX_TRIES):
            try:
                resp = self.http.request(method, self.base + path, headers=headers, data=data, timeout=60)
            except Exception as exc:  # network trouble: try again like a 5xx
                last = f"couldn't reach Miara ({type(exc).__name__})"
                if attempt + 1 < MAX_TRIES:
                    self.sleep(min(2 ** attempt, 30))
                    continue
                raise BridgeError(last) from exc
            code = resp.status_code
            if code == 429:
                err = _error(resp)
                if err.get("code") == "daily_limit":
                    raise DailyLimit(err.get("message") or "Daily limit reached. It resets at midnight UTC.")
                wait = _retry_after(resp)
                last = "rate limited"
                if attempt + 1 < MAX_TRIES:
                    self.sleep(wait)
                    continue
                raise BridgeError(f"Still rate limited after {MAX_TRIES} tries. Run it again in a few minutes.")
            if code >= 500:
                last = f"Miara answered {code}"
                if attempt + 1 < MAX_TRIES:
                    self.sleep(min(2 ** attempt, 30))  # 1, 2, 4, 8 seconds
                    continue
                raise BridgeError(f"{last} {MAX_TRIES} times. Nothing was changed. Run it again later.")
            if code >= 400:
                err = _error(resp)
                raise BridgeError(f"{err.get('code', code)}: {err.get('message', 'the request was refused')}")
            if str((getattr(resp, 'headers', None) or {}).get('Idempotent-Replayed', '')).lower() == 'true':
                self.replayed += 1
            return resp.json()
        raise BridgeError(last or "failed")


def _error(resp) -> dict:
    try:
        return (resp.json() or {}).get("error") or {}
    except Exception:
        return {}


def _retry_after(resp) -> float:
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        return max(1.0, min(float(raw), 3600.0))
    except (TypeError, ValueError):
        return 5.0


def declare_metrics(client: Client, readings: list[dict], state: dict) -> list[str]:
    """Declare the x.* metrics these readings use, once. Re-declares one if its definition changed."""
    used = {r["metric"] for r in readings if r["metric"].startswith("x.")}
    done = state.setdefault("declared", {})
    declared_now = []
    for d in DECLARED_METRICS:
        if d["metric"] not in used:
            continue
        fingerprint = hashlib.sha256(canonical(d).encode()).hexdigest()[:16]
        if done.get(d["metric"]) == fingerprint:
            continue
        client.request("POST", "/metrics", d)
        done[d["metric"]] = fingerprint
        declared_now.append(d["metric"])
    return declared_now


def fingerprint(row: dict) -> str:
    return hashlib.sha256(canonical(row).encode()).hexdigest()[:16]


def unsent(rows: list[dict], state: dict, key) -> list[dict]:
    """Only the rows that changed since they were last sent. A bridge that re-reads the last
    two days every minute would otherwise send the same rows hundreds of times a day."""
    sent = state.get("sent", {})
    return [r for r in rows if sent.get(key(r), {}).get("fp") != fingerprint(r)]


def mark_sent(rows: list[dict], state: dict, key, now: str) -> None:
    sent = state.setdefault("sent", {})
    for r in rows:
        sent[key(r)] = {"fp": fingerprint(r), "at": now}


def entry_key(e: dict) -> str:
    return "e:" + e["source_event_id"]


def reading_key(r: dict) -> str:
    return f"r:{r['child_id']}:{r['metric']}:{r['started_at']}"


def send(client: Client, child_id: str | None, entries: list[dict], readings: list[dict], state: dict | None = None,
         now: str | None = None) -> dict:
    """Send what changed, entries then readings, in batches. Returns a summary dict (see print_summary).

    Each row may carry its own child_id (one bridge, several children); `child_id` fills in the rest.
    """
    state = state if state is not None else {}
    now = now or iso(datetime.now(timezone.utc))
    entries = [dict(e, child_id=e.get("child_id") or child_id) for e in entries]
    readings = [dict(r, child_id=r.get("child_id") or child_id) for r in readings]
    entries, readings = unsent(entries, state, entry_key), unsent(readings, state, reading_key)
    replayed_before = client.replayed
    summary = {"replayed": 0, "entries": {s: 0 for s in STATUSES}, "reasons": [], "readings_saved": 0, "readings_received": 0, "declared": []}
    for batch in batches(entries):
        res = client.request("POST", "/events:batch", {"entries": batch})
        done = set()
        for r in res.get("results", []):
            status = r.get("status", "rejected")
            summary["entries"][status] = summary["entries"].get(status, 0) + 1
            if status in ("suggested", "skipped", "rejected"):
                summary["reasons"].append((status, r.get("source_event_id"), r.get("error") or _STATUS_WORDS[status]))
            if status != "rejected":
                done.add(r.get("source_event_id"))
            note_kept(r, state, summary)
        mark_sent([e for e in batch if e["source_event_id"] in done], state, entry_key, now)
    if readings:
        summary["declared"] = declare_metrics(client, readings, state)
        for batch in batches(readings):
            res = client.request("POST", "/observations:batch", {"readings": batch})
            summary["readings_saved"] += res.get("saved", 0)
            summary["readings_received"] += res.get("received", len(batch))
            mark_sent(batch, state, reading_key, now)
    summary["replayed"] = client.replayed - replayed_before
    summary["sent"] = len(entries) + len(readings)
    return summary


def note_kept(result: dict, state: dict, summary: dict) -> None:
    """Remember what someone changed in Miara (the result's `kept`), and say when a sleep joined a timer.

    Later sends don't move a kept start, end or wakes back, so a sleep someone ended stays ended
    however long Nanit keeps it open; is_asleep() reads this so the bridge stops treating it as open.
    """
    sid = result.get("source_event_id")
    if result.get("joined"):
        summary["reasons"].append(("joined", sid, "someone was timing this sleep in Miara; it joined their timer"))
    if result.get("kept"):
        state.setdefault("kept", {})[sid] = {
            "fields": result["kept"], "started_at": result.get("started_at"), "ended_at": result.get("ended_at")}
        summary["reasons"].append(("kept", sid, "someone changed " + ", ".join(result["kept"]) + " in Miara; that stays"))


def ended_in_miara(state: dict, source_event_id: str) -> str | None:
    """When someone ended this sleep in Miara: its end as Miara has it, else None."""
    k = state.get("kept", {}).get(source_event_id)
    return k["ended_at"] if k and "ended_at" in k["fields"] else None


_STATUS_WORDS = {
    "suggested": "overlaps a sleep someone logged; it waits for them to answer",
    "skipped": "someone deleted it in Miara or answered its suggestion, so it isn't brought back",
}


def print_summary(summary: dict, local_problems: list[tuple[str, str]], out=print) -> None:
    e = summary["entries"]
    total = sum(e.values())
    out(f"Entries:  {total} sent. " + ", ".join(f"{e[s]} {s}" for s in STATUSES))
    out(f"Readings: {summary['readings_received']} sent, {summary['readings_saved']} new or changed")
    if summary.get("replayed"):
        out(f"{summary['replayed']} request(s) were the same as one sent in the last 24 hours, so Miara returned the first answer "
            "above and saved nothing twice.")
    for m in summary["declared"]:
        out(f"Declared metric {m}")
    for status, sid, why in summary["reasons"]:
        out(f"  {status}: {sid}: {why}")
    for sid, why in local_problems:
        out(f"  rejected before sending: {sid}: {why}")


# ---------------------------------------------------------------------------
# State and command line
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def pick_sandbox_child(client: Client) -> str:
    kids = client.request("GET", "/children").get("children", [])
    if not kids:
        raise BridgeError("The sandbox has no children right now. Try again after its nightly reset.")
    return kids[0]["id"]


# ---------------------------------------------------------------------------
# One sync, watch, setup, probe
# ---------------------------------------------------------------------------

def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def resolve_children(client: Client | None, sandbox: bool, cli_child: str | None) -> dict:
    spec = os.environ.get("MIARA_CHILDREN", "") or cli_child or os.environ.get("MIARA_CHILD_ID", "")
    children = parse_children(spec)
    if not children and sandbox and client is not None:
        children = {"*": pick_sandbox_child(client)}
    return children


async def sync_once(n, client: Client | None, state: dict, children: dict, now: datetime, dry_run: bool = False,
                    quiet: bool = False) -> dict | None:
    items = await fetch_async(n, state, now)
    if dry_run and not children:
        children = {"*": "MIARA_CHILDREN-not-set"}  # so a dry run still shows what it would send
    entries, readings = map(items, state, children)
    entries, readings, problems = check(entries, readings, {d["metric"] for d in DECLARED_METRICS})
    if dry_run:
        kinds: dict[str, int] = {}
        for e in entries:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        what = ", ".join(f"{n} {k}" for k, n in kinds.items()) or "no entries"
        print(f"Read Nanit: {len(items)} baby(ies), {what}, {len(readings)} reading(s) from the last "
              f"{int(LOOKBACK.total_seconds() // 3600)} hours.")
        for e in entries:
            print("  entry  ", canonical(e))
        for r in readings:
            print("  reading", canonical(r))
        for sid, why in problems:
            print(f"  would reject: {sid}: {why}")
        for label, count in (state.get("unread_logs") or {}).items():
            print(f"  not read: {count} Nanit {label}. Run probe and open an issue with its output.")
        print("Dry run: nothing was sent.")
        return None
    summary = await asyncio.to_thread(send, client, None, entries, readings, state, iso(now))
    if summary["sent"] or problems or not quiet:
        prefix = f"{stamp()} " if quiet else ""
        if summary["sent"]:
            print_summary(summary, problems, out=lambda s: print(prefix + s))
        elif not quiet:
            print("Nothing new to send.")
    state["last_sync"] = iso(now)
    return summary


async def wait_for_next(client: Client, seconds: float, state: dict) -> str:
    """Wait `seconds`, or less if Miara says someone wants fresh data (the app opened, a pull to refresh).

    Miara holds GET /wait open for up to 25 seconds and answers early with {"woken": true}.
    An older Miara without /wait gets a plain sleep.
    """
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 1:
        if state.get("wait_unsupported"):
            await asyncio.sleep(left)
            return "timeout"
        try:
            res = await asyncio.to_thread(client.request, "GET", f"/wait?timeout={int(min(left, 25))}")
        except BridgeError as exc:
            if "not_found" in str(exc) or "404" in str(exc):
                state["wait_unsupported"] = True
                continue
            await asyncio.sleep(min(left, 30))
            continue
        if res.get("woken"):
            return "woken"
    return "timeout"


def next_interval(state: dict, now: datetime) -> int:
    if is_asleep(state):
        return WATCH_ASLEEP
    if recently_active(state, now):
        return WATCH_ACTIVE
    return WATCH_QUIET


async def watch(n, client: Client, state: dict, state_path: Path, children: dict) -> int:
    for stream in (sys.stdout, sys.stderr):  # a line at a time, so launchd's log keeps up
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(line_buffering=True)
    print(f"{stamp()} Watching Nanit. Every {WATCH_ASLEEP}s while asleep, {WATCH_ACTIVE}s after activity, "
          f"{WATCH_QUIET}s when quiet, and right away when Miara asks. Ctrl-C to stop.")
    backoff = 0
    while True:
        now = datetime.now(timezone.utc)
        try:
            await sync_once(n, client, state, children, now, quiet=True)
            save_state(state_path, state)
            backoff = 0
            wait = next_interval(state, now)
        except DailyLimit as exc:
            print(f"{stamp()} Stopped for now: {exc}", file=sys.stderr)
            wait = 3600
        except (BridgeError, nanit.NanitError) as exc:
            backoff = min(max(backoff * 2, 60), WATCH_MAX_BACKOFF)
            print(f"{stamp()} {exc} Trying again in {backoff // 60 or 1} min.", file=sys.stderr)
            wait = backoff
        why = await wait_for_next(client, wait, state)
        if why == "woken":
            print(f"{stamp()} Miara asked for fresh data.")


def match_children(babies: list, kids: list[dict]) -> dict:
    """Nanit baby uid → Miara child id: by first name, or the only one of each."""
    kids = [k for k in kids if not k.get("history_only")]
    first = lambda name: (name or "").split(" ")[0].strip().lower()  # noqa: E731
    out = {}
    for b in babies:
        same = [k for k in kids if first(k.get("name")) and first(k.get("name")) == first(b.name)]
        if len(same) == 1:
            out[b.uid] = same[0]["id"]
    if not out and len(babies) == 1 and len(kids) == 1:
        out[babies[0].uid] = kids[0]["id"]
    return out


def write_env_value(key: str, value: str, path: Path = ENV_FILE) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [l for l in lines if not l.strip().startswith(key + "=")]
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def shape(value, depth: int = 0):
    """Field names and types only: enough to see what Nanit sends, nothing about your baby."""
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in value.items()} if depth < 4 else "{…}"
    if isinstance(value, list):
        return [shape(value[0], depth + 1), f"×{len(value)}"] if value else []
    if isinstance(value, str) and value.isupper() and len(value) < 32:
        return value  # event keys and types like FELL_ASLEEP are the point
    return type(value).__name__


async def probe(n) -> int:
    now = int(time.time())
    for b in await n.babies():
        print(f"Baby {b.uid}: camera {'yes' if b.camera_uid else 'no'}")
        feed = await n.feed(b.uid, now - 172800, now)
        keys: dict[str, int] = {}
        for e in feed:
            k = str(e.get('key') or e.get('type'))
            keys[k] = keys.get(k, 0) + 1
        print(f"  events: {len(feed)} in 48 h: {keys}")
        if feed:
            print("  event shape:", json.dumps(shape(feed[0])))
        cal = await n.calendar(b.uid, now - 172800, now)
        types: dict[str, int] = {}
        for c in cal:
            types[c.get("type")] = types.get(c.get("type"), 0) + 1
        print(f"  calendar: {len(cal)} items in 48 h: {types}")
        sleeps = [c for c in cal if c.get("type") == "auto_sleep"]
        if sleeps:
            print("  auto_sleep shape:", json.dumps(shape(sleeps[0])))
        await probe_raw(n, b.uid, now)
        t, h = await n.room(b)
        print(f"  room: temperature {'yes' if t is not None else 'no'}, humidity {'yes' if h is not None else 'no'}")
    return 0


async def probe_raw(n, uid: str, now: int) -> None:
    """Ask Nanit the same things a few ways and print only the shape of each answer."""
    day = 86400
    tries = [
        ("/calendar (ms)", f"/babies/{uid}/calendar", {"start": (now - 2 * day) * 1000, "end": now * 1000}),
        ("/messages", f"/babies/{uid}/messages", {"limit": 20}),
    ]
    for label, path, params in tries:
        try:
            body = await n.get(path, params)
        except Exception as err:  # noqa: BLE001 - a probe reports and moves on
            print(f"  raw {label}: {str(err).split(':')[0]}")
            continue
        print(f"  raw {label}: {json.dumps(shape(body))[:600]}")


def status(guide) -> int:
    state = load_state(HERE / "state.json")
    last = state.get("last_sync")
    if guide.running():
        print("Running in the background. It starts again when you log in.")
    else:
        print("Not running in the background. Start it with ./install.sh.")
    if sys.platform == "darwin":
        tips = guide.always_on_tips()
        for t in tips:
            print(f"  • {t}")
    if last:
        mins = int((datetime.now(timezone.utc) - parse_time(last)).total_seconds() // 60)
        print(f"Last checked Nanit {mins} min ago." if mins < 120 else f"Last checked Nanit {mins // 60} h ago.")
    try:
        tail = (HERE / "bridge.log").read_text().splitlines()[-5:]
    except FileNotFoundError:
        tail = []
    if tail:
        print("Latest from bridge.log:")
        for line in tail:
            print("  " + line)
    return 0


def main(argv: list[str] | None = None, http=None, sleep=time.sleep, now=None, nanit_rest=None) -> int:
    ap = argparse.ArgumentParser(description="Send a Nanit camera's sleeps and room readings to Miara.")
    ap.add_argument("command", nargs="?", default="sync", choices=["sync", "watch", "setup", "probe", "status", "stop"])
    ap.add_argument("--sandbox", action="store_true", help="made-up Nanit data into Miara's public sandbox family")
    ap.add_argument("--dry-run", action="store_true", help="read Nanit and print what would be sent; send nothing")
    ap.add_argument("--child", help="Miara child id for every Nanit baby (or set MIARA_CHILDREN)")
    ap.add_argument("--state", help="state file (default state.json, or state.sandbox.json with --sandbox)")
    args = ap.parse_args(argv)

    if args.command in ("setup", "status", "stop"):
        import guide
        if args.command == "setup":
            return guide.run()
        if args.command == "stop":
            print("Stopped. It won't start again at login." if guide.stop_background() else "It wasn't running.")
            return 0
        return status(guide)

    base = os.environ.get("MIARA_BASE", DEFAULT_BASE)
    token = SANDBOX_TOKEN if args.sandbox else os.environ.get("MIARA_TOKEN", "")
    state_path = Path(args.state) if args.state else HERE / ("state.sandbox.json" if args.sandbox else "state.json")
    rest = nanit_rest or (SampleNanit(now) if args.sandbox else None)

    async def run() -> int:
        async with nanit.Nanit(rest=rest) as n:
            if args.command == "probe":
                return await probe(n)
            if not token and not args.dry_run:
                raise BridgeError("No Miara token. Make one in the app (Profile → Siri, assistants and connections → "
                                  "New connection), then put MIARA_TOKEN=... in .env. To try it without one, run with --sandbox.")
            client = Client(base, token, http=http, sleep=sleep) if token else None
            children = resolve_children(client, args.sandbox, args.child)
            state = load_state(state_path)
            if args.dry_run:
                await sync_once(n, client, state, children, now or datetime.now(timezone.utc), dry_run=True)
                return 0
            if not children:
                raise BridgeError("The bridge doesn't know which Miara child to write to. Run: ./install.sh")
            if args.command == "watch":
                return await watch(n, client, state, state_path, children)
            await sync_once(n, client, state, children, now or datetime.now(timezone.utc))
            save_state(state_path, state)
            return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except DailyLimit as exc:
        print(f"Stopped: {exc}\nNothing is lost: the next run sends what this one couldn't.", file=sys.stderr)
        return 3
    except (BridgeError, nanit.NanitError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
