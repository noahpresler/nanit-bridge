"""Guided setup: `./install.sh`, or `python bridge.py setup` to run it again.

Four steps, each skipped if it's already done: sign in to Nanit, connect Miara, choose the
child, then check what it would send, send it, and keep it running. Nothing goes to Miara
until you say yes.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import bridge
import nanit

HERE = Path(__file__).resolve().parent
LABEL = "local.nanit-bridge"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = HERE / "bridge.log"
STEPS = 4


class IO:
    """Questions and answers. Swapped for a script in tests."""

    def __init__(self, ask=input, secret=getpass.getpass, out=print):
        self.ask, self.secret, self.out = ask, secret, out

    def yes(self, question: str, default: bool = True) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self.ask(f"{question} [{hint}] ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.out("  Type y or n.")

    def choose(self, question: str, options: list[str]) -> int | None:
        """1-based pick from options, or None for the last line ("none of these")."""
        for i, o in enumerate(options, 1):
            self.out(f"    {i}. {o}")
        self.out(f"    {len(options) + 1}. None of these")
        while True:
            answer = self.ask(f"{question} ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(options) + 1:
                return None if int(answer) == len(options) + 1 else int(answer) - 1
            self.out(f"  Type a number from 1 to {len(options) + 1}.")


def step(io: IO, n: int, title: str) -> None:
    io.out(f"\nStep {n} of {STEPS}: {title}")


def done(io: IO, text: str) -> None:
    io.out(f"  ✓ {text}")


def names(babies) -> str:
    return ", ".join((b.name or "").strip() or b.uid for b in babies)


# ---------------------------------------------------------------------------
# Step 1: Nanit
# ---------------------------------------------------------------------------

async def current_babies():
    try:
        nanit.load_session()
        async with nanit.Nanit() as n:
            return await n.babies()
    except nanit.NanitError:
        return None


def sign_in_to_nanit(io: IO, signed_in=None, sign_in=None) -> list:
    step(io, 1, "Sign in to Nanit")
    babies = signed_in if signed_in is not None else asyncio.run(current_babies())
    if babies and io.yes(f"  You're already signed in. Nanit has: {names(babies)}. Keep this sign-in?"):
        return babies
    io.out("  Your password is used once, here, and never saved. What's saved is the sign-in Nanit\n"
           f"  gives back, in {nanit.session_path()}, readable only by you.")
    sign_in = sign_in or (lambda e, p, code: asyncio.run(nanit.setup(e, p, code)))
    for attempt in range(3):
        email = io.ask("  Nanit email: ").strip()
        password = io.secret("  Nanit password (hidden as you type): ")
        try:
            babies = sign_in(email, password, lambda: io.ask("  Nanit just sent you a code. Enter it: "))
        except nanit.NanitError as exc:
            io.out(f"  {exc}")
            if attempt < 2:
                io.out("  Try again.")
            continue
        if not babies:
            raise bridge.BridgeError("Signed in, but this Nanit account has no babies.")
        done(io, f"Signed in. Nanit has: {names(babies)}.")
        return babies
    raise bridge.BridgeError("Couldn't sign in to Nanit. Run ./install.sh again when you're ready.")


# ---------------------------------------------------------------------------
# Step 2: Miara token
# ---------------------------------------------------------------------------

def miara_children(token: str, http=None) -> list[dict]:
    client = bridge.Client(os.environ.get("MIARA_BASE", bridge.DEFAULT_BASE), token, http=http)
    return [k for k in client.request("GET", "/children").get("children", []) if not k.get("history_only")]


def connect_miara(io: IO, env_file: Path, http=None) -> tuple[str, list[dict]]:
    step(io, 2, "Connect to Miara")
    token = os.environ.get("MIARA_TOKEN", "")
    if token:
        try:
            kids = miara_children(token, http)
            if kids and io.yes(f"  Using the Miara connection saved earlier (it writes to {kid_names(kids)}). Keep it?"):
                return token, kids
        except bridge.BridgeError:
            io.out("  The Miara token saved earlier doesn't work any more. Make a new one.")
    io.out("  In the Miara app:\n"
           "    1. Open Profile → Siri, assistants and connections → New connection.\n"
           "    2. Choose \"Choose what it can do\" and turn on all three: read, add entries, add readings.\n"
           "    3. Name it Nanit. Under For, pick the child Nanit watches.\n"
           "    4. Tap Create token, then copy it.")
    for attempt in range(3):
        token = io.secret("  Paste the token here (hidden as you paste), then press Return: ").strip()
        if not token.startswith("miara_pat_"):
            io.out("  That isn't a Miara token. It starts with miara_pat_.")
            continue
        try:
            kids = miara_children(token, http)
        except bridge.BridgeError as exc:
            io.out(f"  Miara didn't accept it: {exc}")
            continue
        if not kids:
            io.out("  That token isn't for any child. Make a new one and choose your child.")
            continue
        bridge.write_env_value("MIARA_TOKEN", token, env_file)
        os.environ["MIARA_TOKEN"] = token
        done(io, f"Connected. It can write to {kid_names(kids)}.")
        return token, kids
    raise bridge.BridgeError("Couldn't connect to Miara. Run ./install.sh again with a new token.")


def kid_names(kids: list[dict]) -> str:
    return ", ".join(k.get("name") or k["id"] for k in kids)


# ---------------------------------------------------------------------------
# Step 3: which child is which
# ---------------------------------------------------------------------------

def choose_children(io: IO, babies, kids: list[dict], env_file: Path) -> dict:
    step(io, 3, "Choose the child")
    current = bridge.parse_children(os.environ.get("MIARA_CHILDREN", ""))
    ids = {k["id"] for k in kids}
    if current and all(c in ids for c in current.values()) and all(b.uid in current for b in babies):
        pairs = "; ".join(f"Nanit's {(b.name or b.uid).strip()} → Miara's {name_of(kids, current[b.uid])}" for b in babies)
        if io.yes(f"  Saved earlier: {pairs}. Keep it?"):
            return current
    guess = bridge.match_children(babies, kids)
    out: dict = {}
    for b in babies:
        label = (b.name or b.uid).strip()
        if b.uid in guess and io.yes(f"  Nanit's {label} is Miara's {name_of(kids, guess[b.uid])}. Right?"):
            out[b.uid] = guess[b.uid]
            continue
        i = io.choose(f"  Which Miara child is Nanit's {label}?", [k.get("name") or k["id"] for k in kids])
        if i is not None:
            out[b.uid] = kids[i]["id"]
    if not out:
        raise bridge.BridgeError("No child chosen, so there's nowhere to send sleeps. Run ./install.sh again to choose.")
    bridge.write_env_value("MIARA_CHILDREN", ",".join(f"{u}={c}" for u, c in out.items()), env_file)
    os.environ["MIARA_CHILDREN"] = ",".join(f"{u}={c}" for u, c in out.items())
    done(io, "Saved.")
    return out


def name_of(kids: list[dict], child_id: str) -> str:
    return next((k.get("name") or child_id for k in kids if k["id"] == child_id), child_id)


# ---------------------------------------------------------------------------
# Step 4: check, send, keep running
# ---------------------------------------------------------------------------

def local(at: str) -> datetime:
    return bridge.parse_time(at).astimezone()


def describe(entry: dict) -> str:
    start = local(entry["started_at"])
    when = start.strftime("%a %-I:%M %p")
    if not entry.get("ended_at"):
        return f"{when}, still asleep"
    end = local(entry["ended_at"])
    mins = round((end - start).total_seconds() / 60)
    length = f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"
    end_txt = end.strftime("%-I:%M %p" if end.date() == start.date() else "%a %-I:%M %p")
    return f"{when} to {end_txt}  ({length})"


async def preview(children: dict, state: dict, now: datetime, n=None):
    async def read(n):
        items = await bridge.fetch_async(n, state, now)
        entries, readings = bridge.map(items, state, children)
        entries, readings, _ = bridge.check(entries, readings, {d["metric"] for d in bridge.DECLARED_METRICS})
        return entries, readings
    if n is not None:
        return await read(n)
    async with nanit.Nanit() as real:
        return await read(real)


def check_and_send(io: IO, token: str, children: dict, state_path: Path, http=None, nanit_client=None, now=None) -> bool:
    step(io, 4, "Check, then start")
    now = now or datetime.now(timezone.utc)
    state = bridge.load_state(state_path)
    io.out("  Reading the last two days from Nanit…")
    entries, readings = asyncio.run(preview(children, state, now, nanit_client))
    sleeps = [e for e in entries if e["kind"] == "sleep"]
    if sleeps:
        io.out(f"  Nanit has {len(sleeps)} sleep{'s' if len(sleeps) != 1 else ''} to add to Miara:")
        for e in sorted(sleeps, key=lambda e: e["started_at"]):
            io.out(f"    {describe(e)}")
    else:
        io.out("  Nanit has no new sleeps from the last two days. New ones will come in as they happen.")
    if readings:
        io.out("  And the nursery's temperature and humidity.")
    if entries or readings:
        if not io.yes("  Add these to Miara now?"):
            io.out("  Nothing was sent.")
            return False
        client = bridge.Client(os.environ.get("MIARA_BASE", bridge.DEFAULT_BASE), token, http=http)
        summary = bridge.send(client, None, entries, readings, state, bridge.iso(now))
        bridge.save_state(state_path, state)
        e = summary["entries"]
        added = e.get("saved", 0) + e.get("updated", 0) + e.get("unchanged", 0)
        done(io, f"Added {added} sleep{'s' if added != 1 else ''}." if sleeps else "Sent the room readings.")
        if e.get("suggested"):
            io.out(f"  {e['suggested']} overlapped a sleep someone already logged. Miara will ask which times to keep.")
        for status, sid, why in summary["reasons"]:
            io.out(f"  Not added ({status}): {why}")
    else:
        state["last_sync"] = bridge.iso(now)
        bridge.save_state(state_path, state)
    return True


# ---------------------------------------------------------------------------
# Keep running
# ---------------------------------------------------------------------------

def plist_text(python: str = sys.executable) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{python}</string><string>{HERE / 'bridge.py'}</string><string>watch</string></array>
  <key>WorkingDirectory</key><string>{HERE}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>{LOG}</string>
  <key>StandardErrorPath</key><string>{LOG}</string>
</dict>
</plist>
"""


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def running() -> bool:
    return platform.system() == "Darwin" and launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0


def start_in_background() -> None:
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(plist_text())
    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    res = launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST))
    if res.returncode != 0:
        raise bridge.BridgeError(f"macOS didn't start it: {res.stderr.strip() or res.stdout.strip()}")


def stop_background() -> bool:
    was = running()
    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    PLIST.unlink(missing_ok=True)
    return was


def keep_running(io: IO) -> None:
    if platform.system() != "Darwin":
        io.out("\n  To keep it running on Linux, see schedule/nanit-bridge.service.\n"
               f"  Or run it by hand: {sys.executable} {HERE / 'bridge.py'} watch")
        return
    if running():
        start_in_background()  # pick up the new settings
        done(io, "It was already running in the background. Restarted it with these settings.")
        return
    if not io.yes("\n  Keep it running in the background, so new sleeps show up on their own?"):
        io.out(f"  OK. Run it when you like: {sys.executable} {HERE / 'bridge.py'} watch")
        return
    start_in_background()
    done(io, "Running. It starts again when you log in, and checks Nanit every minute while the baby sleeps.")
    io.out("  macOS may show a notice that a background item was added. That's this.")


# ---------------------------------------------------------------------------

def run(io: IO | None = None, env_file: Path | None = None, state_path: Path | None = None) -> int:
    io = io or IO()
    env_file = env_file or bridge.ENV_FILE
    state_path = state_path or HERE / "state.json"
    io.out("Nanit to Miara setup\n"
           "About three minutes. Nothing goes to Miara until you say yes.")
    try:
        babies = sign_in_to_nanit(io)
        token, kids = connect_miara(io, env_file)
        children = choose_children(io, babies, kids, env_file)
        check_and_send(io, token, children, state_path)
        keep_running(io)
    except (KeyboardInterrupt, EOFError):
        io.out("\n\nStopped. Run ./install.sh to pick up where you left off.")
        return 130
    except (bridge.BridgeError, nanit.NanitError) as exc:
        io.out(f"\n  {exc}")
        return 1
    io.out("\nAll set. To check on it: .venv/bin/python bridge.py status")
    return 0
