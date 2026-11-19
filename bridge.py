#!/usr/bin/env python3
"""A bridge from your own device's data to Miara.

Three parts:

  fetch(since)  YOURS. Get what's new from your device or service. Ships as a stub.
  map(items)    YOURS. Turn what fetch returned into Miara entries and readings.
  send(...)     DONE. Batches, idempotency keys, retries, limits, a summary.

Run it:   python bridge.py --sandbox            (public sandbox, made-up family)
          python bridge.py --sandbox --dry-run  (show what it would send, send nothing)
          python bridge.py                      (your family: needs MIARA_TOKEN, MIARA_CHILD_ID)

Your device login stays in your own environment. This file never sends it to Miara.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_BASE = "https://getmiara.com/api/v1"
SANDBOX_TOKEN = "miara_pat_5a4d0b0c5a4d0b0c5a4d0b0c5a4d0b0c5a4d0b0c"

# What this bridge's data is called in Miara. Becomes the prefix of every source_event_id,
# and the middle part of any metric you declare: x.<SOURCE>.<metric>.
SOURCE = "sample"

# Metrics of your own. Numbers in a range, or 2 to 12 lowercase choices. Never free text.
# Declared once, on the first run that uses them. Delete this one if you don't need it.
DECLARED_METRICS = [
    {"metric": "x.sample.score", "label": "Sleep score", "unit": "points", "min": 0, "max": 100},
]


# True while fetch() below is still the made-up sample. A bridge that is still the sample
# refuses to send to a real family. Set it to False when you've written your own fetch().
USING_SAMPLE_DATA = True


# ---------------------------------------------------------------------------
# 1. fetch: REPLACE THIS
# ---------------------------------------------------------------------------

def fetch(since: datetime | None) -> list[dict]:
    """Return what's new since `since` (a UTC datetime, or None on the first run).

    REPLACE THIS. The code below returns made-up sample data so the template runs as it is.

    Your own version reads from wherever your data lives: the service's API, an export file,
    a local database. Use your own login from the environment (os.environ["DEVICE_PASSWORD"],
    a .env file kept out of git), never from this repo, and never send it to Miara.

    Return plain dicts. Keep each item's own id from the service: map() builds the
    source_event_id from it, so the same item always lands on the same entry.
    """
    day = (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    at = lambda h, m=0: (day + timedelta(hours=h, minutes=m)).isoformat()  # noqa: E731
    tag = day.strftime("%Y%m%d")
    items = [
        {"id": f"sleep-{tag}", "type": "sleep", "start": at(2), "end": at(8, 30), "updated_at": at(8, 30)},
        {"id": f"bottle-{tag}", "type": "bottle", "at": at(10, 15), "ml": 120, "milk": "formula", "updated_at": at(10, 15)},
        {"id": f"feed-{tag}", "type": "nursing", "at": at(12), "left_s": 480, "right_s": 360, "updated_at": at(12, 15)},
        {"id": f"diaper-{tag}", "type": "diaper", "at": at(13, 30), "kind": "wet", "updated_at": at(13, 30)},
        {"id": f"pump-{tag}", "type": "pump", "at": at(15), "ml": 90, "updated_at": at(15)},
        {"id": f"score-{tag}", "type": "score", "at": at(8, 30), "points": 82, "updated_at": at(8, 30)},
    ]
    # A room sensor: one temperature reading every 10 minutes for an hour.
    for i in range(7):
        items.append({"id": f"room-{tag}-{i}", "type": "room", "at": at(2, 10 * i), "celsius": round(21.0 + i * 0.1, 1),
                      "updated_at": at(2, 10 * i)})
    # The night's stages, as the device saw them.
    for i, (stage, a, b) in enumerate([("light", (2, 0), (2, 40)), ("deep", (2, 40), (4, 10)), ("rem", (4, 10), (5, 0))]):
        items.append({"id": f"stage-{tag}-{i}", "type": "stage", "stage": stage, "start": at(*a), "end": at(*b),
                      "updated_at": at(*b)})
    if since is not None:
        items = [i for i in items if parse_time(i["updated_at"]) >= since]
    return items


# ---------------------------------------------------------------------------
# 2. map: REPLACE THIS TOO
# ---------------------------------------------------------------------------

def map(items: list[dict]) -> tuple[list[dict], list[dict]]:  # noqa: A001 (shadows the builtin on purpose)
    """Turn fetched items into (entries, readings). child_id is added for you in send().

    Entries are things that happened: kind is sleep, bottle, nursing, diaper or pump.
        {"source_event_id", "kind", "started_at", "ended_at" (sleeps), "details", "raw"}
    Readings are measurements: a core metric (heart_rate, spo2, room_temp_c, sleep_stage ...)
    or one you declared above. Numbers or one of a fixed set of words, nothing else.
        {"metric", "started_at", "ended_at" (sleep_stage), "value"}

    Always build source_event_id from the service's own id with event_id(), never from
    the time you ran or a counter. Details are checked like the app checks them:
    https://getmiara.com/developers
    """
    entries: list[dict] = []
    readings: list[dict] = []
    for it in items:
        t = it.get("type")
        if t == "sleep":
            entries.append({"source_event_id": event_id(it["id"]), "kind": "sleep",
                            "started_at": iso(it["start"]), "ended_at": iso(it["end"]), "details": {}})
        elif t == "bottle":
            entries.append({"source_event_id": event_id(it["id"]), "kind": "bottle", "started_at": iso(it["at"]),
                            "details": {"volume_ml": it["ml"], "contents": {"formula": "formula", "breast": "breast_milk"}.get(it["milk"], "mixed")}})
        elif t == "nursing":
            entries.append({"source_event_id": event_id(it["id"]), "kind": "nursing", "started_at": iso(it["at"]),
                            "details": {"left_seconds": it["left_s"], "right_seconds": it["right_s"]}})
        elif t == "diaper":
            entries.append({"source_event_id": event_id(it["id"]), "kind": "diaper", "started_at": iso(it["at"]),
                            "details": {"diaper": it["kind"]}})
        elif t == "pump":
            entries.append({"source_event_id": event_id(it["id"]), "kind": "pump", "started_at": iso(it["at"]),
                            "details": {"total_ml": it["ml"]}})
        elif t == "room":
            readings.append({"metric": "room_temp_c", "started_at": iso(it["at"]), "value": it["celsius"]})
        elif t == "stage":
            readings.append({"metric": "sleep_stage", "started_at": iso(it["start"]), "ended_at": iso(it["end"]), "value": it["stage"]})
        elif t == "score":
            readings.append({"metric": "x.sample.score", "started_at": iso(it["at"]), "value": it["points"]})
        # Anything else is left out on purpose. Don't send what you can't map to a number or a choice.
    return entries, readings


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


def send(client: Client, child_id: str, entries: list[dict], readings: list[dict], state: dict | None = None) -> dict:
    """Send entries then readings in batches. Returns a summary dict (see print_summary)."""
    state = state if state is not None else {}
    replayed_before = client.replayed
    summary = {"replayed": 0, "entries": {s: 0 for s in STATUSES}, "reasons": [], "readings_saved": 0, "readings_received": 0, "declared": []}
    for batch in batches(entries):
        body = {"entries": [dict(e, child_id=child_id) for e in batch]}
        res = client.request("POST", "/events:batch", body)
        for r in res.get("results", []):
            status = r.get("status", "rejected")
            summary["entries"][status] = summary["entries"].get(status, 0) + 1
            if status in ("suggested", "skipped", "rejected"):
                summary["reasons"].append((status, r.get("source_event_id"), r.get("error") or _STATUS_WORDS[status]))
    if readings:
        summary["declared"] = declare_metrics(client, readings, state)
        for batch in batches(readings):
            body = {"readings": [dict(r, child_id=child_id) for r in batch]}
            res = client.request("POST", "/observations:batch", body)
            summary["readings_saved"] += res.get("saved", 0)
            summary["readings_received"] += res.get("received", len(batch))
    summary["replayed"] = client.replayed - replayed_before
    return summary


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


def main(argv: list[str] | None = None, http=None, sleep=time.sleep, now=None) -> int:
    ap = argparse.ArgumentParser(description="Send your own device's data to Miara.")
    ap.add_argument("--sandbox", action="store_true", help="use the public sandbox token and its first child (made-up family)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and map, print what would be sent, send nothing, keep state as it is")
    ap.add_argument("--child", help="child id (or set MIARA_CHILD_ID)")
    ap.add_argument("--since", help="fetch from this ISO time instead of the last sync")
    ap.add_argument("--full", action="store_true", help="ignore the last sync and fetch everything (resends come back unchanged)")
    ap.add_argument("--state", help="state file (default state.json, or state.sandbox.json with --sandbox)")
    args = ap.parse_args(argv)

    base = os.environ.get("MIARA_BASE", DEFAULT_BASE)
    token = SANDBOX_TOKEN if args.sandbox else os.environ.get("MIARA_TOKEN", "")
    child = args.child or os.environ.get("MIARA_CHILD_ID", "")
    state_path = Path(args.state or ("state.sandbox.json" if args.sandbox else "state.json"))
    started = (now or datetime.now(timezone.utc))

    try:
        if not token and not args.dry_run:
            raise BridgeError("No token. Make one in the app (Profile → Siri, assistants and connections → New connection), "
                              "then set MIARA_TOKEN. To try it without one, run with --sandbox.")
        if USING_SAMPLE_DATA and not args.sandbox and not args.dry_run:
            raise BridgeError("fetch() is still the made-up sample, so this won't send it to a real family. "
                              "Write your own fetch() and map(), then set USING_SAMPLE_DATA = False. "
                              "To try the sample, run with --sandbox.")
        client = Client(base, token, http=http, sleep=sleep)
        state = load_state(state_path)
        if args.since:
            since = parse_time(args.since)
        elif args.full or not state.get("last_sync"):
            since = None
        else:
            since = parse_time(state["last_sync"])

        items = fetch(since)
        entries, readings = map(items)
        declared = {d["metric"] for d in DECLARED_METRICS}
        entries, readings, problems = check(entries, readings, declared)
        print(f"Fetched {len(items)} item(s) since {iso(since) if since else 'the beginning'}: "
              f"{len(entries)} entries, {len(readings)} readings.")

        if args.dry_run:
            for e in entries[:20]:
                print("  entry  ", canonical(e))
            for r in readings[:20]:
                print("  reading", canonical(r))
            for sid, why in problems:
                print(f"  would reject: {sid}: {why}")
            print("Dry run: nothing was sent and the last sync time is unchanged.")
            return 0

        if not entries and not readings:
            print("Nothing new to send.")
        else:
            if not child:
                if args.sandbox:
                    child = pick_sandbox_child(client)
                else:
                    raise BridgeError("No child. Set MIARA_CHILD_ID (or pass --child). GET /children lists the ids this token is for.")
            summary = send(client, child, entries, readings, state)
            print_summary(summary, problems)
        state["last_sync"] = iso(started)
        save_state(state_path, state)
        return 0
    except DailyLimit as exc:
        print(f"Stopped: {exc}\nThe last sync time was not moved, so the next run picks up where this one stopped.", file=sys.stderr)
        return 3
    except BridgeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
