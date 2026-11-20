"""The bridge on Vercel: a setup page at / and a scheduled check at /api/sync.

Everything it keeps (the Nanit session, the Miara token, which child, what was sent) goes in a
private Blob store in the owner's own Vercel account. Nothing is logged that isn't a count.
Set BRIDGE_SANDBOX=1 to try it with made-up Nanit data and Miara's public sandbox.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

import auth
import bridge
import link
import nanit
import store as stores
from page import PAGE

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

CONFIG, STATE, STATUS, LINK = "config.json", "state.json", "status.json", "link.json"
SCOPES = ["write:events", "write:observations"]
BUSY = "A check is running right now. Try again in a minute."


def secret() -> str | None:
    return os.environ.get("CRON_SECRET")


def sandbox() -> bool:
    return os.environ.get("BRIDGE_SANDBOX") == "1"


def db() -> stores.Store:
    return stores.active() or stores.FileStore(bridge.HERE / ".vercel-local")


def base() -> str:
    return os.environ.get("MIARA_BASE", bridge.DEFAULT_BASE)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fail(message: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=code)


# -- config ------------------------------------------------------------------------------------

def load_config() -> dict:
    cfg = db().get_json(CONFIG) or {}
    if sandbox():
        cfg["miara_token"] = bridge.SANDBOX_TOKEN
        if "sandbox_anchor" not in cfg:
            cfg["sandbox_anchor"] = int(time.time())
    return cfg


def save_config(cfg: dict) -> None:
    cfg = {k: v for k, v in cfg.items() if not (sandbox() and k == "miara_token")}
    db().put_json(CONFIG, cfg)


def miara(cfg: dict) -> bridge.Client:
    return bridge.Client(base(), cfg.get("miara_token", ""))


def nanit_rest(cfg: dict):
    """In sandbox mode: a made-up baby whose times stay put, so a second check finds nothing new."""
    if not sandbox():
        return None
    return bridge.SampleNanit(datetime.fromtimestamp(cfg["sandbox_anchor"], timezone.utc))


def kids_of(client: bridge.Client) -> list[dict]:
    return [{"id": k["id"], "name": k.get("name") or k["id"]}
            for k in client.request("GET", "/children").get("children", []) if not k.get("history_only")]


def has_session() -> bool:
    try:
        nanit.load_session()
        return True
    except nanit.NanitError:
        return False


# -- who's asking ------------------------------------------------------------------------------

def authed(request: Request) -> bool:
    return auth.cookie_ok(request.cookies.get(auth.COOKIE), secret())


def guard(request: Request) -> JSONResponse | None:
    """None when the caller may use setup; otherwise the answer to send back."""
    problem = auth.secret_problem(secret())
    if problem:
        return fail(problem, 403)
    if not authed(request):
        return fail("Enter the setup password first.", 401)
    origin = request.headers.get("origin")
    if request.method != "GET" and origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        return fail("That request came from somewhere else.", 403)
    return None


def secure(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"


# -- one check ---------------------------------------------------------------------------------

def summarize(summary: dict | None) -> dict:
    e = (summary or {}).get("entries") or {}
    return {"saved": e.get("saved", 0), "updated": e.get("updated", 0), "unchanged": e.get("unchanged", 0)}


def record(ok: bool, error: str | None = None, summary: dict | None = None) -> None:
    status = db().get_json(STATUS) or {}
    stamp = bridge.iso(now_utc())
    status["last_check"] = stamp
    if ok:
        status["last_ok"], status["last_error"] = stamp, None
        status["last_sent"] = summarize(summary)
    else:
        status["last_error"] = error
    db().put_json(STATUS, status)


async def check_once(cfg: dict, now: datetime) -> dict | None:
    """One sync_once. The caller holds the lock. Saves state, even when the check stops partway."""
    bridge.INCLUDE_NANIT_LOGS = bool(cfg.get("include_logs", True))
    state = db().get_json(STATE) or {}
    client = miara(cfg)
    try:
        async with nanit.Nanit(rest=nanit_rest(cfg)) as n:
            return await bridge.sync_once(n, client, state, cfg["children"], now, quiet=True)
    finally:
        db().put_json(STATE, state)


async def run_sync() -> tuple[int, dict]:
    cfg = load_config()
    if not cfg.get("enabled"):
        return 200, {"ok": True, "skipped": "Setup isn't finished, so there is nothing to check."}
    try:
        with db().leased(ttl=270):
            try:
                summary = await check_once(cfg, now_utc())
            except (bridge.BridgeError, nanit.NanitError) as exc:
                record(False, str(exc))
                return 502, {"ok": False, "error": str(exc)}
            record(True, summary=summary)
            return 200, {"ok": True, **summarize(summary)}
    except stores.Busy:
        return 200, {"ok": True, "skipped": "Another check is running."}


@app.get("/api/sync")
async def sync(request: Request):
    if not auth.bearer_ok(request.headers.get("authorization"), secret()):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    code, body = await run_sync()
    return JSONResponse(body, status_code=code)


# -- the setup page ----------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                       "Referrer-Policy": "no-referrer"})


@app.post("/api/login")
async def login(request: Request):
    problem = auth.secret_problem(secret())
    if problem:
        return fail(problem, 403)
    body = await request.json()
    if not auth.password_ok(str(body.get("password", "")), secret()):
        await asyncio.sleep(1)
        return fail("That isn't the password you chose in Vercel.", 401)
    res = JSONResponse({"ok": True})
    res.set_cookie(auth.COOKIE, auth.make_cookie(secret()), max_age=auth.COOKIE_SECONDS, httponly=True,
                   secure=secure(request), samesite="strict", path="/")
    return res


@app.post("/api/logout")
async def logout():
    res = JSONResponse({"ok": True})
    res.delete_cookie(auth.COOKIE, path="/")
    return res


@app.get("/api/status")
async def status(request: Request):
    problem = auth.secret_problem(secret())
    if problem:
        return {"locked": problem}
    if not authed(request):
        return {"signed_in": False, "sandbox": sandbox()}
    cfg = load_config()
    st = db().get_json(STATUS) or {}
    return {"signed_in": True, "sandbox": sandbox(),
            "nanit": sandbox() or has_session(), "miara": bool(cfg.get("miara_token")),
            "child": bool(cfg.get("children")), "started": bool(cfg.get("enabled")),
            "last_check": st.get("last_check"), "last_ok": st.get("last_ok"),
            "last_error": st.get("last_error"), "last_sent": st.get("last_sent"),
            "include_logs": cfg.get("include_logs", True)}


def locked(fn):
    """Setup changes wait their turn behind a running check."""
    async def inner(request: Request):
        denied = guard(request)
        if denied:
            return denied
        try:
            with db().leased(ttl=120):
                return await fn(request)
        except stores.Busy:
            return fail(BUSY, 409)
    inner.__name__ = fn.__name__
    return inner


@app.post("/api/nanit/signin")
@locked
async def nanit_signin(request: Request):
    if sandbox():
        return fail("Test mode uses made-up Nanit data, so there is nothing to sign in to.")
    body = await request.json()
    email, password = str(body.get("email", "")).strip(), str(body.get("password", ""))
    if not email or not password:
        return fail("Enter your Nanit email and password.")
    try:
        res = await nanit.sign_in(email, password, body.get("mfa_token") or None, body.get("code"))
    except nanit.NanitError as exc:
        return fail(str(exc))
    except Exception:
        return fail("Couldn't reach Nanit. Try again in a minute.", 502)
    if "mfa_token" in res:
        return {"mfa_token": res["mfa_token"]}
    if not res["babies"]:
        return fail("Signed in, but this Nanit account has no babies.")
    return {"babies": [{"uid": b.uid, "name": b.name} for b in res["babies"]]}


@app.post("/api/miara/start")
async def miara_start(request: Request):
    denied = guard(request)
    if denied:
        return denied
    try:
        made = await asyncio.to_thread(link.start, bridge.Client(base(), ""), "Nanit bridge", SCOPES)
    except bridge.BridgeError as exc:
        return fail(f"Miara didn't start a sign-in link: {exc}", 502)
    expires = int(made.get("expires_in", 600))
    db().put_json(LINK, {"token": made["token"], "expires_at": time.time() + expires,
                         "interval": int(made.get("interval", 3))})
    return {"code": made["code"], "url": made["url"], "expires_in": expires,
            "interval": int(made.get("interval", 3))}


@app.get("/api/miara/poll")
async def miara_poll(request: Request):
    denied = guard(request)
    if denied:
        return denied
    pending = db().get_json(LINK)
    if not pending:
        return {"status": "expired"}
    if time.time() > pending["expires_at"] + 5:
        db().delete(LINK)
        return {"status": "expired"}
    client = bridge.Client(base(), pending["token"])
    try:
        got = (await asyncio.to_thread(client.request, "GET", "/links")).get("status")
        if got != "approved":
            if got in ("denied", "expired"):
                db().delete(LINK)
            return {"status": got or "pending"}
        kids = await asyncio.to_thread(kids_of, client)
    except bridge.BridgeError as exc:
        return fail(f"Miara: {exc}", 502)
    db().delete(LINK)
    if not kids:
        return fail("That approval isn't for any child. Start again and choose your child.")
    cfg = load_config()
    cfg["miara_token"] = pending["token"]
    save_config(cfg)
    return {"status": "approved", "kids": kids}


@app.post("/api/miara/token")
async def miara_token(request: Request):
    denied = guard(request)
    if denied:
        return denied
    token = str((await request.json()).get("token", "")).strip()
    if not token.startswith("miara_pat_"):
        return fail("That isn't a Miara token. It starts with miara_pat_.")
    try:
        kids = await asyncio.to_thread(kids_of, bridge.Client(base(), token))
    except bridge.BridgeError as exc:
        return fail(f"Miara didn't accept it: {exc}")
    if not kids:
        return fail("That token isn't for any child. Make a new one and choose your child.")
    cfg = load_config()
    cfg["miara_token"] = token
    save_config(cfg)
    return {"kids": kids}


@app.get("/api/children")
async def children_get(request: Request):
    denied = guard(request)
    if denied:
        return denied
    cfg = load_config()
    if not cfg.get("miara_token"):
        return fail("Connect to Miara first.")
    try:
        if sandbox():
            async with nanit.Nanit(rest=nanit_rest(cfg)) as n:
                babies = await n.babies()
        else:
            async with nanit.Nanit() as n:
                babies = await n.babies()
        kids = await asyncio.to_thread(kids_of, miara(cfg))
    except (nanit.NanitError, bridge.BridgeError) as exc:
        return fail(str(exc), 502)
    guess = bridge.match_children(babies, kids)
    saved = cfg.get("children") or {}
    return {"babies": [{"uid": b.uid, "name": b.name} for b in babies], "kids": kids,
            "chosen": {b.uid: saved.get(b.uid) or guess.get(b.uid) for b in babies},
            "include_logs": cfg.get("include_logs", True)}


@app.post("/api/children")
async def children_set(request: Request):
    denied = guard(request)
    if denied:
        return denied
    body = await request.json()
    chosen = {u: c for u, c in (body.get("children") or {}).items() if c}
    if not chosen:
        return fail("Choose a child, so there is somewhere to send sleeps.")
    cfg = load_config()
    cfg["children"] = chosen
    cfg["include_logs"] = bool(body.get("include_logs", True))
    save_config(cfg)
    return {"ok": True}


def detail(e: dict) -> str:
    d = e.get("details") or {}
    kind = e["kind"]
    if kind == "diaper":
        return f"diaper, {d.get('diaper', '')}"
    if kind == "bottle":
        return f"bottle, {d.get('volume_ml', 0):g} ml {str(d.get('contents', '')).replace('_', ' ')}".strip()
    if kind == "nursing":
        return (f"nursing, left {round(d.get('left_seconds', 0) / 60)} min, "
                f"right {round(d.get('right_seconds', 0) / 60)} min")
    if kind == "pump":
        amount = d.get("total_ml") or (d.get("left_ml", 0) + d.get("right_ml", 0))
        return "pumping" + (f", {amount:g} ml" if amount else "")
    return "sleep"


@app.get("/api/preview")
async def preview(request: Request):
    denied = guard(request)
    if denied:
        return denied
    cfg = load_config()
    if not cfg.get("children"):
        return fail("Choose the child first.")
    bridge.INCLUDE_NANIT_LOGS = bool(cfg.get("include_logs", True))
    state = db().get_json(STATE) or {}
    now = now_utc()
    try:
        async with nanit.Nanit(rest=nanit_rest(cfg)) as n:
            items = await bridge.fetch_async(n, state, now)
    except (nanit.NanitError, bridge.BridgeError) as exc:
        return fail(str(exc), 502)
    entries, readings = bridge.map(items, state, cfg["children"])
    entries, readings, _ = bridge.check(entries, readings, {d["metric"] for d in bridge.DECLARED_METRICS})
    rows = [{"kind": e["kind"], "started_at": e["started_at"], "ended_at": e.get("ended_at"), "detail": detail(e)}
            for e in sorted(entries, key=lambda e: e["started_at"])]
    return {"entries": rows, "readings": bool(readings),
            "unread": [{"label": k, "count": v} for k, v in (state.get("unread_logs") or {}).items()]}


@app.post("/api/start")
@locked
async def start(request: Request):
    cfg = load_config()
    if not cfg.get("children") or not cfg.get("miara_token"):
        return fail("Finish the earlier steps first.")
    try:
        summary = await check_once(cfg, now_utc())
    except bridge.DailyLimit as exc:
        record(False, str(exc))
        return fail(f"{exc} Nothing is lost: the next check sends the rest.", 429)
    except (bridge.BridgeError, nanit.NanitError) as exc:
        record(False, str(exc))
        return fail(str(exc), 502)
    cfg["enabled"] = True
    save_config(cfg)
    record(True, summary=summary)
    return {"ok": True, **summarize(summary)}


@app.post("/api/disconnect")
@locked
async def disconnect(request: Request):
    nanit.forget_session()
    for name in (CONFIG, STATE, STATUS, LINK):
        db().delete(name)
    return {"ok": True}
