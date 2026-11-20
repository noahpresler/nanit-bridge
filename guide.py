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
import link
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


def connect_miara(io: IO, env_file: Path, http=None, sign_in=None) -> tuple[str, list[dict]]:
    step(io, 2, "Connect to Miara")
    token = os.environ.get("MIARA_TOKEN", "")
    if token:
        try:
            kids = miara_children(token, http)
            if kids and io.yes(f"  Using the Miara connection saved earlier (it writes to {kid_names(kids)}). Keep it?"):
                return token, kids
        except bridge.BridgeError:
            io.out("  The Miara connection saved earlier doesn't work any more. Connect again.")
    try:
        token, kids = (sign_in or sign_in_to_miara)(io, http)
    except (link.LinkError, bridge.BridgeError) as exc:
        io.out(f"  Couldn't connect by signing in: {exc}")
        return paste_token(io, env_file, http)
    if not kids:
        io.out("  That connection isn't for any child. Try again and choose your child.")
        return paste_token(io, env_file, http)
    bridge.write_env_value("MIARA_TOKEN", token, env_file)
    os.environ["MIARA_TOKEN"] = token
    done(io, f"Connected. It can write to {kid_names(kids)}.")
    return token, kids


def sign_in_to_miara(io: IO, http=None) -> tuple[str, list[dict]]:
    """Opens Miara in the browser (or prints the link where none can open) and waits for approval."""
    client = bridge.Client(os.environ.get("MIARA_BASE", bridge.DEFAULT_BASE), "", http=http)
    say = lambda text: io.out("\n".join("  " + line for line in str(text).splitlines()))  # noqa: E731
    token, kids = link.connect(client, name="Nanit bridge", scopes=["write:events", "write:observations"], out=say)
    return token, [k for k in kids if not k.get("history_only")]


def paste_token(io: IO, env_file: Path, http=None) -> tuple[str, list[dict]]:
    """The fallback: a token made in the app, pasted here."""
    io.out("  Or make a token in the Miara app:\n"
           "    1. Open Profile → AI Assistants, Apps, & Devices → New connection.\n"
           "    2. Choose \"A device bridge\". Name it Nanit. Under For, pick the child Nanit watches.\n"
           "    3. Tap Create token, then copy it.")
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
    raise bridge.BridgeError("Couldn't connect to Miara. Run ./install.sh again.")


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


NOUN = {"sleep": ("sleep", "sleeps"), "diaper": ("diaper", "diapers"), "bottle": ("bottle", "bottles"),
        "nursing": ("nursing session", "nursing sessions"), "pump": ("pumping session", "pumping sessions")}


def count(n: int, kind: str) -> str:
    one, many = NOUN.get(kind, (kind, kind + "s"))
    return f"{n} {one if n == 1 else many}"


def describe(entry: dict) -> str:
    start = local(entry["started_at"])
    when = start.strftime("%a %-I:%M %p")
    d = entry.get("details") or {}
    kind = entry["kind"]
    if kind == "diaper":
        return f"{when}  diaper, {d['diaper']}"
    if kind == "bottle":
        return f"{when}  bottle, {d['volume_ml']:g} ml {d['contents'].replace('_', ' ')}"
    if kind == "nursing":
        return f"{when}  nursing, left {round(d['left_seconds'] / 60)} min, right {round(d['right_seconds'] / 60)} min"
    if kind == "pump":
        amount = d.get("total_ml") or (d.get("left_ml", 0) + d.get("right_ml", 0))
        return f"{when}  pumping" + (f", {amount:g} ml" if amount else "")
    if not entry.get("ended_at"):
        return f"{when}  sleep, still asleep"
    end = local(entry["ended_at"])
    mins = round((end - start).total_seconds() / 60)
    length = f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"
    end_txt = end.strftime("%-I:%M %p" if end.date() == start.date() else "%a %-I:%M %p")
    return f"{when}  sleep to {end_txt} ({length})"


def ask_about_logs(io: IO, env_file: Path) -> None:
    current = os.environ.get("NANIT_INCLUDE_LOGS")
    if current in ("0", "1") and io.yes(
            "  Saved earlier: " + ("add" if current == "1" else "don't add")
            + " what's logged in the Nanit app. Keep that?"):
        bridge.INCLUDE_NANIT_LOGS = current == "1"
        return
    on = io.yes("  Also add what you log by hand in the Nanit app (diapers, bottles, nursing, pumping, naps)?")
    if on:
        io.out("  Log each one in one app, not both: Miara can't tell that a diaper logged twice is the same one.")
    bridge.write_env_value("NANIT_INCLUDE_LOGS", "1" if on else "0", env_file)
    os.environ["NANIT_INCLUDE_LOGS"] = "1" if on else "0"
    bridge.INCLUDE_NANIT_LOGS = on


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


def check_and_send(io: IO, token: str, children: dict, state_path: Path, http=None, nanit_client=None, now=None,
                   env_file: Path | None = None) -> bool:
    step(io, 4, "Check, then start")
    now = now or datetime.now(timezone.utc)
    state = bridge.load_state(state_path)
    if env_file is not None:
        ask_about_logs(io, env_file)
    io.out("  Reading the last two days from Nanit…")
    entries, readings = asyncio.run(preview(children, state, now, nanit_client))
    if entries:
        kinds: dict[str, int] = {}
        for e in entries:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        io.out("  Nanit has " + ", ".join(count(n, k) for k, n in kinds.items()) + " to add to Miara:")
        for e in sorted(entries, key=lambda e: e["started_at"]):
            io.out(f"    {describe(e)}")
    else:
        io.out("  Nothing new in Nanit from the last two days. New sleeps and logs will come in as they happen.")
    for label, n in (state.get("unread_logs") or {}).items():
        io.out(f"  Not added: {n} Nanit {label} entr{'y' if n == 1 else 'ies'} this bridge can't read yet.")
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
        done(io, f"Added {added} entr{'y' if added == 1 else 'ies'}." if entries else "Sent the room readings.")
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


def _cmd(run, *args: str) -> tuple[int, str]:
    try:
        res = run(list(args), capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        return 1, ""
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def always_on_tips(run=subprocess.run) -> list[str]:
    """What would stop this Mac running the bridge unattended, as plain steps. Empty if nothing."""
    tips: list[str] = []
    _, batt = _cmd(run, "pmset", "-g", "batt")
    laptop = "InternalBattery" in batt
    _, custom = _cmd(run, "pmset", "-g", "custom")
    ac = custom.split("AC Power:", 1)[-1]
    sleep = next((line.split()[1] for line in ac.splitlines() if line.split()[:1] == ["sleep"]), "0")
    if sleep != "0":
        tips.append("It sleeps when idle, and the bridge stops while it sleeps. In System Settings → "
                    + ("Battery → Options, turn on \"Prevent automatic sleeping on power adapter when the display is off\"."
                       if laptop else "Energy, turn on \"Prevent automatic sleeping when the display is off\"."))
    if laptop:
        return tips  # a laptop runs it whenever it's open and you're logged in; that's fine
    autorestart = next((line.split()[1] for line in custom.splitlines() if line.split()[:1] == ["autorestart"]), "1")
    if autorestart == "0":
        tips.append("After a power cut it stays off. In System Settings → Energy, turn on "
                    "\"Start up automatically after a power failure\".")
    auto_login = _cmd(run, "defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser")[0] == 0
    filevault = "FileVault is On" in _cmd(run, "fdesetup", "status")[1]
    if not auto_login:
        if filevault:
            tips.append("After a restart, it waits until you log in: FileVault needs your password first. "
                        "macOS updates unlock it for you. For a restart of your own, run "
                        "sudo fdesetup authrestart and it comes back on its own.")
        else:
            tips.append("After a restart, it waits until you log in. To start without you, in System Settings → "
                        "Users & Groups, set \"Automatically log in as\" to you.")
    return tips


def show_tips(io: IO, tips: list[str]) -> None:
    if not tips:
        done(io, "This Mac is set to stay on and come back after a restart.")
        return
    io.out("  To keep it running without you:")
    for t in tips:
        io.out(f"    • {t}")


def keep_running(io: IO) -> None:
    if platform.system() != "Darwin":
        io.out("\n  To keep it running on Linux, see schedule/nanit-bridge.service.\n"
               f"  Or run it by hand: {sys.executable} {HERE / 'bridge.py'} watch")
        return
    if running():
        start_in_background()  # pick up the new settings
        done(io, "It was already running in the background. Restarted it with these settings.")
        show_tips(io, always_on_tips())
        return
    if not io.yes("\n  Keep it running in the background, so new sleeps show up on their own?"):
        io.out(f"  OK. Run it when you like: {sys.executable} {HERE / 'bridge.py'} watch")
        return
    start_in_background()
    done(io, "Running. It starts again when you log in, and checks Nanit every minute while the baby sleeps.")
    io.out("  macOS may show a notice that a background item was added. That's this.")
    show_tips(io, always_on_tips())


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
        check_and_send(io, token, children, state_path, env_file=env_file)
        keep_running(io)
    except (KeyboardInterrupt, EOFError):
        io.out("\n\nStopped. Run ./install.sh to pick up where you left off.")
        return 130
    except (bridge.BridgeError, nanit.NanitError) as exc:
        io.out(f"\n  {exc}")
        return 1
    io.out("\nAll set. To check on it: .venv/bin/python bridge.py status")
    return 0
