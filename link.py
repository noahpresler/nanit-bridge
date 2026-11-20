"""Connect a bridge to a Miara family by signing in, instead of copying a token.

    python bridge.py setup

opens Miara in your browser, like `gh auth login`. You sign in, check the code matches the
one in the terminal, choose the children and approve; the bridge sees that within a few
seconds and saves its token to .env as MIARA_TOKEN (and MIARA_CHILD_ID when there's one
child). Where no browser can open (over SSH, on a computer with no screen), it prints the
link to open anywhere else.

The token is made for this bridge and shown only to it. It shows in the Miara app under
Profile like any connection, where it can be turned off, or everything it added undone.
"""
from __future__ import annotations

import os
import sys
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    """Read KEY=value lines from .env into the environment, without replacing what's already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def write_env_value(key: str, value: str, path: Path = ENV_FILE) -> None:
    """Set KEY=value in .env, keeping the other lines, readable only by you."""
    lines = path.read_text().splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)


def has_browser() -> bool:
    """Whether a browser can open here: a Mac, Windows, or Linux with a desktop."""
    if sys.platform in ("darwin", "win32"):
        return not os.environ.get("SSH_CONNECTION")
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class LinkError(Exception):
    pass


def start(client, name: str, scopes: list[str]) -> dict:
    """POST /links: {token, code, url, expires_in, interval}."""
    return client.request("POST", "/links", {"name": name, "scopes": scopes})


def wait(client, link: dict, sleep=time.sleep, clock=time.monotonic) -> str:
    """Polls GET /links with the link's token until it's answered or runs out: approved, denied or expired."""
    client.token = link["token"]
    interval = max(1, int(link.get("interval", 3)))
    deadline = clock() + int(link.get("expires_in", 600)) + interval
    while clock() < deadline:
        status = client.request("GET", "/links").get("status")
        if status in ("approved", "denied", "expired"):
            return status
        sleep(interval)
    return "expired"


def connect(client, name: str = "Device bridge", scopes: list[str] | None = None, out=print, sleep=time.sleep,
            clock=time.monotonic, open_browser=None) -> tuple[str, list[dict]]:
    """Gets a token by sign-in link. Returns it with the children it may write to."""
    scopes = scopes or ["write:events", "write:observations"]
    link = start(client, name, scopes)
    url, code = link["url"], link["code"]
    opened = False
    if open_browser is None and has_browser():
        open_browser = webbrowser.open
    if open_browser:
        try:
            opened = bool(open_browser(url))
        except Exception:
            opened = False
    if opened:
        out(f"Opened Miara in your browser. Sign in, check the code is {code}, and approve {name}.")
        out(f"If it didn't open, go to {url}")
    else:
        out(f"Open this link in a browser, sign in to Miara and approve {name}:")
        out(f"  {url}\n  Code: {code}")
    out(f"Waiting for you to approve… (the link lasts {int(link.get('expires_in', 600)) // 60} minutes)")
    status = wait(client, link, sleep=sleep, clock=clock)
    if status == "denied":
        raise LinkError("It was turned down in Miara. Run setup again to start over.")
    if status != "approved":
        raise LinkError("The link ran out before anyone approved it. Run setup again for a new one.")
    kids = [k for k in client.request("GET", "/children").get("children", []) if not k.get("history_only")]
    return link["token"], kids


def setup(client, env_file: Path = ENV_FILE, name: str = "Device bridge", scopes: list[str] | None = None,
          out=print, **kw) -> int:
    """`python bridge.py setup`: connect, then save MIARA_TOKEN (and MIARA_CHILD_ID for one child) to .env."""
    try:
        token, kids = connect(client, name, scopes, out=out, **kw)
    except LinkError as exc:
        out(str(exc))
        return 1
    write_env_value("MIARA_TOKEN", token, env_file)
    names = ", ".join(k.get("name", "?") for k in kids) or "no children"
    out(f"\nConnected as “{name}”. It writes to {names}.")
    if len(kids) == 1:
        write_env_value("MIARA_CHILD_ID", kids[0]["id"], env_file)
    else:
        for k in kids:
            out(f"  {k.get('name', '?')}: {k['id']}")
        out("Set MIARA_CHILD_ID in .env to the child your device watches, or send child_id with each row.")
    out(f"Saved to {env_file.name}. Turn it off any time in the Miara app: Profile → AI Assistants, Apps, & Devices.")
    return 0
