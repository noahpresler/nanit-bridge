"""Nanit's own (unofficial) API: sign-in, sleep events and room readings.

Nanit has no public API. These are the calls its app makes, as mapped by the community
(https://github.com/wealthystudent/ha-nanit). They can change without notice.

Your Nanit session lives in one file on this machine (default ~/.config/nanit-bridge/session.json,
readable only by you). It holds a refresh token, which is full access to your Nanit account:
keep it out of git, backups you share, and screenshots. Your Nanit password is never stored.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import store

# aionanit warns on every camera retry; a camera that's off is reported once, as "no room readings".
logging.getLogger("aionanit").setLevel(logging.ERROR)

API = "https://api.nanit.com"
HEADERS = {"nanit-api-version": "1", "Accept": "application/json"}

# Feed event keys that say where the baby is. Anything else (MOTION, SOUND, VISIT…) is ignored.
SLEEP_KEYS = {"FELL_ASLEEP", "PUT_TO_SLEEP", "WOKE_UP", "REMOVED_ASLEEP", "PUT_IN_BED", "REMOVED", "LEAVING"}


class NanitError(Exception):
    """Something to tell the person running the bridge."""


def session_path() -> Path:
    raw = os.environ.get("NANIT_SESSION")
    if raw:
        return Path(raw).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "nanit-bridge" / "session.json"


SESSION_KEY = "session.json"


def _blobs(path: Path | None):
    """The Blob store, when running on Vercel and no file was asked for. None on a computer."""
    return store.active() if path is None and not os.environ.get("NANIT_SESSION") else None


def read_session_text(path: Path | None = None) -> str:
    """The saved session as text. Raises FileNotFoundError when there isn't one."""
    blobs = _blobs(path)
    if blobs is not None:
        raw = blobs.get(SESSION_KEY)
        if raw is None:
            raise FileNotFoundError(SESSION_KEY)
        return raw.decode()
    return (path or session_path()).read_text()


def load_session(path: Path | None = None) -> dict:
    where = path or session_path()
    try:
        data = json.loads(read_session_text(path))
    except FileNotFoundError:
        raise NanitError("Not signed in to Nanit. Run: ./install.sh") from None
    except ValueError:
        raise NanitError(f"{where} isn't readable. Run: ./install.sh") from None
    if not data.get("refresh_token"):
        raise NanitError("The Nanit session has no refresh token. Run: ./install.sh")
    return data


def save_session(data: dict, path: Path | None = None) -> None:
    """Write the session readable only by you, replacing the old one in one step."""
    blobs = _blobs(path)
    if blobs is not None:
        blobs.put(SESSION_KEY, json.dumps(data).encode())
        return
    path = path or session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


@dataclass(frozen=True)
class Baby:
    uid: str
    name: str
    camera_uid: str


class Nanit:
    """One signed-in Nanit account. Async; owns an aiohttp session and aionanit's client.

    `rest` can be swapped for a fake in tests: an object with `get(path, params) -> dict`.
    """

    def __init__(self, session_file: Path | None = None, rest=None):
        self.session_file = session_file  # None: the default place (a file, or Blob on Vercel)
        self._rest_override = rest
        self._http = None
        self._client = None
        self._cameras: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------------------------

    async def __aenter__(self) -> "Nanit":
        if self._rest_override is not None:
            return self
        import aiohttp
        from aionanit import NanitClient

        saved = load_session(self.session_file)
        self._http = aiohttp.ClientSession()
        self._client = NanitClient(self._http)
        self._client.restore_tokens(saved.get("access_token", ""), saved["refresh_token"])
        # Nanit rotates the refresh token on every refresh. Save each new one, or the next
        # run signs in with a dead token.
        self._client.token_manager.on_tokens_refreshed(self._tokens_refreshed)
        return self

    async def __aexit__(self, *exc) -> None:
        for cam in self._cameras.values():
            try:
                await cam.async_stop()
            except Exception:
                pass
        if self._http is not None:
            await self._http.close()

    def _tokens_refreshed(self, access_token: str, refresh_token: str) -> None:
        data = {"access_token": access_token, "refresh_token": refresh_token}
        try:
            old = json.loads(read_session_text(self.session_file))
            data = {**old, **data}
        except (FileNotFoundError, ValueError):
            pass
        save_session(data, self.session_file)

    # -- REST --------------------------------------------------------------------------------

    async def get(self, path: str, params: dict | None = None) -> dict:
        if self._rest_override is not None:
            return self._rest_override.get(path, params or {})
        from aionanit import NanitAuthError

        tm = self._client.token_manager
        for attempt in range(2):
            token = await tm.async_get_access_token()
            async with self._http.get(API + path, params=params, timeout=30,
                                      headers={**HEADERS, "Authorization": token}) as resp:
                if resp.status == 401 and attempt == 0:
                    await tm.async_force_refresh(token)
                    continue
                if resp.status == 401:
                    raise NanitError("Nanit signed this bridge out. Run: ./install.sh")
                if resp.status == 429:
                    raise NanitError("Nanit is rate limiting this bridge. It will try again later.")
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    raise NanitError(f"Nanit answered {resp.status} for {path}: {body}")
                return await resp.json()
        raise NanitError("Nanit sign-in failed")  # unreachable

    async def babies(self) -> list[Baby]:
        body = await self.get("/babies")
        out = []
        for b in body.get("babies", []):
            out.append(Baby(uid=b["uid"], name=b.get("name") or "", camera_uid=b.get("camera_uid") or ""))
        return out

    async def feed(self, baby_uid: str, start: int, end: int) -> list[dict]:
        """The camera's newest events (FELL_ASLEEP, WOKE_UP, VISIT…) between start and end.

        Nanit's /events takes a count, not a range, so the range is applied here.
        """
        body = await self.get(f"/babies/{baby_uid}/events", {"limit": 200})
        return [e for e in body.get("events") or []
                if e.get("time") is not None and start <= float(e["time"]) <= end]

    async def calendar(self, baby_uid: str, start: int, end: int) -> list[dict]:
        """Finished sleeps (auto_sleep, sleep) and anything logged in the Nanit app."""
        body = await self.get(f"/babies/{baby_uid}/calendar", {"start": int(start), "end": int(end)})
        return body.get("calendar") or []

    # -- camera ------------------------------------------------------------------------------

    async def room(self, baby: Baby) -> tuple[float | None, float | None]:
        """(°C, % humidity) from the camera, or (None, None) when it's asleep or unreachable.

        The first call opens a connection to the camera (through Nanit's cloud, or on your
        network when NANIT_CAMERA_IP is set) and keeps it open for later calls.
        """
        if self._rest_override is not None:
            return getattr(self._rest_override, "room", lambda b: (None, None))(baby)
        if not baby.camera_uid:
            return None, None
        cam = self._cameras.get(baby.camera_uid)
        try:
            if cam is None:
                local_ip = os.environ.get("NANIT_CAMERA_IP") or None
                cam = self._client.camera(baby.camera_uid, baby.uid, prefer_local=bool(local_ip), local_ip=local_ip)
                await asyncio.wait_for(cam.async_start(), 30)
                self._cameras[baby.camera_uid] = cam
            sensors = await asyncio.wait_for(cam.async_get_sensor_data(), 20)
        except Exception:
            return None, None
        return sensors.temperature, sensors.humidity


# -- setup ---------------------------------------------------------------------------------

async def setup(email: str, password: str, ask_code) -> list[Baby]:
    """Sign in (with the emailed or texted code) and save the session. Returns the babies."""
    import aiohttp
    from aionanit import NanitAuthError, NanitClient, NanitMfaRequiredError

    async with aiohttp.ClientSession() as http:
        client = NanitClient(http)
        try:
            tokens = await client.async_login(email, password)
        except NanitMfaRequiredError as mfa:
            code = ask_code()
            try:
                tokens = await client.async_verify_mfa(email, password, mfa.mfa_token, code.strip())
            except NanitAuthError:
                raise NanitError("Nanit didn't accept that code. Codes expire quickly: run ./install.sh again and use the newest one.") from None
        except NanitAuthError as exc:
            raise NanitError(f"Nanit didn't accept that email and password ({exc}).") from None
        save_session({"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"]})
        n = Nanit()
        n._http, n._client = http, client
        client.token_manager.on_tokens_refreshed(n._tokens_refreshed)
        return await n.babies()


async def sign_in(email: str, password: str, mfa_token: str | None = None, code: str | None = None) -> dict:
    """Sign in one step at a time, for a web page that can't wait for a typed code.

    First call (email, password): {"mfa_token": ...} when Nanit sent a code, else {"babies": [...]}.
    Second call, with that mfa_token and the code: {"babies": [...]}. The session is saved on success.
    The password is only passed along to Nanit; it is never kept.
    """
    import aiohttp
    from aionanit import NanitAuthError, NanitClient, NanitMfaRequiredError

    async with aiohttp.ClientSession() as http:
        client = NanitClient(http)
        try:
            if mfa_token:
                try:
                    tokens = await client.async_verify_mfa(email, password, mfa_token, (code or "").strip())
                except NanitAuthError:
                    raise NanitError("Nanit didn't accept that code. Codes expire quickly: sign in again and use the newest one.") from None
            else:
                tokens = await client.async_login(email, password)
        except NanitMfaRequiredError as mfa:
            return {"mfa_token": mfa.mfa_token}
        except NanitAuthError as exc:
            raise NanitError(f"Nanit didn't accept that email and password ({exc}).") from None
        save_session({"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"]})
        n = Nanit()
        n._http, n._client = http, client
        client.token_manager.on_tokens_refreshed(n._tokens_refreshed)
        return {"babies": await n.babies()}


def forget_session(path: Path | None = None) -> None:
    blobs = _blobs(path)
    if blobs is not None:
        blobs.delete(SESSION_KEY)
    else:
        (path or session_path()).unlink(missing_ok=True)
