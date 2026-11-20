"""The setup page: one small HTML file, no framework. Every value from the server goes in as text."""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Nanit to Miara</title>
<style>
:root{--bg:#F7F4ED;--surface:#FFFEFA;--ink:#292C29;--muted:#5E625E;--line:#DDD8CB;--olive:#465744;--olive-ink:#FFFEFA;--bad:#8A3B2B}
@media (prefers-color-scheme:dark){:root{--bg:#171C19;--surface:#242B26;--ink:#ECEAE2;--muted:#A5ABA3;--line:#38413B;--olive:#9DB596;--olive-ink:#171C19;--bad:#E0907E}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-text-size-adjust:100%}
main{max-width:520px;margin:0 auto;padding:32px 20px 64px}
h1{font:600 26px/1.2 Georgia,"Times New Roman",serif;margin:0 0 6px;letter-spacing:-.01em}
h2{font:600 20px/1.3 Georgia,"Times New Roman",serif;margin:28px 0 8px}
p{margin:0 0 14px}
.muted{color:var(--muted)}
.small{font-size:14px}
.step{font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin:24px 0 0}
label{display:block;font-weight:600;font-size:15px;margin:16px 0 6px}
input[type=email],input[type=password],input[type=text],input[type=tel],select{width:100%;min-height:48px;padding:10px 14px;border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--ink);font:inherit}
input:focus,select:focus,button:focus-visible,a.btn:focus-visible{outline:2px solid var(--olive);outline-offset:2px}
.btn,button{display:inline-flex;align-items:center;justify-content:center;min-height:48px;padding:10px 22px;border:0;border-radius:10px;background:var(--olive);color:var(--olive-ink);font:600 16px/1.2 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;cursor:pointer;text-decoration:none}
.btn.plain,button.plain{background:transparent;color:var(--ink);border:1px solid var(--line)}
button.link{background:none;color:var(--muted);min-height:48px;padding:0;text-decoration:underline;font-weight:400;font-size:15px}
button[disabled]{opacity:.55;cursor:default}
.row{display:flex;gap:12px;flex-wrap:wrap;margin-top:20px;align-items:center}
.check{display:flex;gap:12px;align-items:flex-start;margin-top:20px}
.check input{width:24px;height:24px;margin:2px 0 0;accent-color:var(--olive);flex:none}
.check label{margin:0;font-weight:400}
.err{color:var(--bad);margin:14px 0 0}
.note{border-left:3px solid var(--olive);padding:2px 0 2px 14px;margin:20px 0}
.code{font:600 34px/1.1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.14em;margin:6px 0 18px}
ul.list{list-style:none;margin:12px 0;padding:0;border-top:1px solid var(--line)}
ul.list li{display:flex;justify-content:space-between;gap:16px;padding:12px 0;border-bottom:1px solid var(--line)}
ul.list span:last-child{color:var(--muted);text-align:right}
dl{margin:12px 0;border-top:1px solid var(--line)}
dl div{display:flex;justify-content:space-between;gap:16px;padding:12px 0;border-bottom:1px solid var(--line)}
dt{color:var(--muted)}dd{margin:0;text-align:right}
details{margin-top:22px}summary{min-height:48px;display:flex;align-items:center;cursor:pointer;color:var(--muted)}
.banner{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 14px;font-size:14px;margin-bottom:16px}
</style>
</head>
<body>
<main id="app" aria-live="polite"><p class="muted">Loading…</p></main>
<script>
"use strict";
const app = document.getElementById("app");
const S = {status: null, babies: [], kids: [], nanitForm: null, view: null, poll: null, busy: false};

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(kid));
  return el;
}
async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  let res, data = {};
  try { res = await fetch(path, opt); } catch (e) { return {ok: false, status: 0, data: {error: "Couldn't reach this page. Check your connection and try again."}}; }
  try { data = await res.json(); } catch (e) {}
  return {ok: res.ok, status: res.status, data};
}
function stopPoll() { if (S.poll) { clearTimeout(S.poll); S.poll = null; } }
function show(...nodes) { stopPoll(); app.replaceChildren(...nodes.flat().filter(n => n != null && n !== false)); window.scrollTo(0, 0); }
function head(step, title, lead) {
  return [S.status && S.status.sandbox ? h("p", {class: "banner"}, "Test mode. Made-up Nanit data goes to Miara's public sandbox, not to a real family.") : null,
    step ? h("p", {class: "step"}, `Step ${step} of 4`) : null, h("h1", {}, title), lead ? h("p", {class: "muted"}, lead) : null];
}
function errBox() { return h("p", {class: "err", role: "alert", id: "err"}); }
function setErr(msg) { const e = document.getElementById("err"); if (e) e.textContent = msg || ""; }
async function busy(btn, label, fn) {
  const was = btn.textContent; btn.disabled = true; btn.textContent = label; setErr("");
  try { await fn(); } finally { btn.disabled = false; btn.textContent = was; }
}
const when = iso => new Date(iso).toLocaleString([], {weekday: "short", hour: "numeric", minute: "2-digit"});
function ago(iso) {
  if (!iso) return "Not yet";
  const m = Math.max(0, Math.round((Date.now() - new Date(iso)) / 60000));
  return m < 1 ? "Just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : new Date(iso).toLocaleDateString();
}

async function load() {
  const r = await api("/api/status");
  S.status = r.data;
  route();
}
function route() {
  const s = S.status;
  if (s.locked) return viewLocked(s.locked);
  if (!s.signed_in) return viewLogin();
  if (S.view) return S.view();
  if (!s.nanit) return viewNanit();
  if (!s.miara) return viewMiara();
  if (!s.child) return viewChild();
  if (!s.started) return viewCheck();
  return viewDone();
}
function go(view) { S.view = view; route(); }
async function next() { S.view = null; await load(); }

function viewLocked(msg) {
  show(h("h1", {}, "Nanit to Miara"), h("p", {class: "note"}, msg));
}
function viewLogin() {
  const pw = h("input", {type: "password", id: "pw", autocomplete: "current-password", required: true});
  const btn = h("button", {type: "submit"}, "Continue");
  const form = h("form", {onsubmit: async e => {
    e.preventDefault();
    await busy(btn, "Checking…", async () => {
      const r = await api("/api/login", {password: pw.value});
      if (!r.ok) return setErr(r.data.error);
      await next();
    });
  }}, h("label", {for: "pw"}, "Setup password"), pw,
  h("p", {class: "muted small"}, "The one you chose when you set this up in Vercel (CRON_SECRET)."), errBox(), h("div", {class: "row"}, btn));
  show(...head(0, "Nanit to Miara", "Sends your Nanit camera's sleeps to Miara."), form);
  pw.focus();
}

function viewNanit() {
  const f = S.nanitForm || {};
  if (f.mfa) return viewCode();
  const email = h("input", {type: "email", id: "em", autocomplete: "username", required: true, value: f.email || ""});
  const pw = h("input", {type: "password", id: "np", autocomplete: "current-password", required: true});
  const btn = h("button", {type: "submit"}, "Sign in to Nanit");
  const form = h("form", {onsubmit: async e => {
    e.preventDefault();
    await busy(btn, "Signing in…", async () => {
      const r = await api("/api/nanit/signin", {email: email.value, password: pw.value});
      if (!r.ok) return setErr(r.data.error);
      if (r.data.mfa_token) { S.nanitForm = {email: email.value, password: pw.value, mfa: r.data.mfa_token}; return viewCode(); }
      S.nanitForm = null; await next();
    });
  }}, h("label", {for: "em"}, "Nanit email"), email, h("label", {for: "np"}, "Nanit password"), pw, errBox(), h("div", {class: "row"}, btn));
  show(...head(1, "Sign in to Nanit", "Your password goes to Nanit once and is never saved. What's saved is the sign-in Nanit gives back, in a private store in your own Vercel account."), form);
  email.focus();
}
function viewCode() {
  const f = S.nanitForm;
  const code = h("input", {type: "text", id: "cd", inputmode: "numeric", autocomplete: "one-time-code", required: true});
  const btn = h("button", {type: "submit"}, "Continue");
  const form = h("form", {onsubmit: async e => {
    e.preventDefault();
    await busy(btn, "Checking…", async () => {
      const r = await api("/api/nanit/signin", {email: f.email, password: f.password, mfa_token: f.mfa, code: code.value});
      if (!r.ok) return setErr(r.data.error);
      S.nanitForm = null; await next();
    });
  }}, h("label", {for: "cd"}, "Code from Nanit"), code, errBox(),
  h("div", {class: "row"}, btn, h("button", {type: "button", class: "link", onclick: () => { S.nanitForm = {email: f.email}; viewNanit(); }}, "Start over")));
  show(...head(1, "Enter the code", "Nanit just sent you a code by text or email. It expires quickly, so use the newest one."), form);
  code.focus();
}

async function viewMiara() {
  const start = h("button", {type: "button"}, "Get a sign-in link");
  const tok = h("input", {type: "password", id: "tk", autocomplete: "off", placeholder: "miara_pat_…"});
  const tokBtn = h("button", {type: "button", class: "plain"}, "Use this token");
  tokBtn.onclick = () => busy(tokBtn, "Checking…", async () => {
    const r = await api("/api/miara/token", {token: tok.value});
    if (!r.ok) return setErr(r.data.error);
    await next();
  });
  const sandbox = S.status.sandbox;
  if (sandbox) {
    show(...head(2, "Connect to Miara", "Test mode is already connected to Miara's public sandbox."),
      h("div", {class: "row"}, h("button", {type: "button", onclick: next}, "Continue")));
    return;
  }
  start.onclick = () => busy(start, "Starting…", async () => {
    const r = await api("/api/miara/start", {});
    if (!r.ok) return setErr(r.data.error);
    viewLink(r.data);
  });
  show(...head(2, "Connect to Miara", "You'll sign in to Miara, check a code, and approve this bridge for your child."),
    errBox(), h("div", {class: "row"}, start),
    h("details", {}, h("summary", {}, "Paste a token instead"),
      h("p", {class: "muted small"}, "In the Miara app: Profile, then AI Assistants, Apps, & Devices, then New connection. Choose \"A device bridge\", name it Nanit, pick the child, and tap Create token."),
      h("label", {for: "tk"}, "Miara token"), tok, h("div", {class: "row"}, tokBtn)));
}
function viewLink(l) {
  const open = h("a", {class: "btn", href: l.url, target: "_blank", rel: "noopener"}, "Open Miara to approve");
  const status = h("p", {class: "muted", id: "ls"}, `Waiting for you to approve. The link lasts ${Math.round(l.expires_in / 60)} minutes.`);
  show(...head(2, "Connect to Miara", "Open Miara, sign in, check that the code matches, choose the child and approve."),
    h("p", {class: "small muted"}, "Code to check"), h("div", {class: "code"}, l.code),
    h("div", {class: "row"}, open), h("div", {style: "margin-top:20px"}, status), errBox(),
    h("div", {class: "row"}, h("button", {type: "button", class: "link", onclick: viewMiara}, "Start over")));
  const tick = async () => {
    const r = await api("/api/miara/poll");
    if (!r.ok) { setErr(r.data.error); return; }
    const st = r.data.status;
    if (st === "approved") return next();
    if (st === "denied") { status.textContent = "It was turned down in Miara."; return; }
    if (st === "expired") { status.textContent = "The link ran out before anyone approved it. Start over for a new one."; return; }
    S.poll = setTimeout(tick, Math.max(2, l.interval || 3) * 1000);
  };
  S.poll = setTimeout(tick, 2500);
}

async function viewChild() {
  show(...head(3, "Choose the child"), h("p", {class: "muted"}, "Loading…"));
  const r = await api("/api/children");
  if (!r.ok) return show(...head(3, "Choose the child"), h("p", {class: "err"}, r.data.error),
    h("div", {class: "row"}, h("button", {type: "button", onclick: viewChild}, "Try again"),
      h("button", {type: "button", class: "plain", onclick: () => go(viewNanit)}, "Sign in to Nanit again")));
  const d = r.data, picks = {};
  const rows = d.babies.map(b => {
    const label = (b.name || "").trim() || "your baby";
    const sel = h("select", {id: "b" + b.uid, "aria-label": `Miara child for Nanit's ${label}`},
      h("option", {value: ""}, d.chosen[b.uid] ? "Skip this one" : "Choose a child"),
      d.kids.map(k => h("option", {value: k.id, selected: d.chosen[b.uid] === k.id}, k.name)));
    picks[b.uid] = sel;
    return h("div", {}, h("label", {for: "b" + b.uid}, `Nanit's ${label} is Miara's`), sel);
  });
  const logs = h("input", {type: "checkbox", id: "lg", checked: d.include_logs});
  const btn = h("button", {type: "button"}, "Continue");
  btn.onclick = () => busy(btn, "Saving…", async () => {
    const children = {}; for (const [uid, sel] of Object.entries(picks)) if (sel.value) children[uid] = sel.value;
    const r2 = await api("/api/children", {children, include_logs: logs.checked});
    if (!r2.ok) return setErr(r2.data.error);
    await next();
  });
  show(...head(3, "Choose the child", d.babies.length > 1 ? "Match each Nanit baby to the Miara child." : "Nanit has one baby on this account."),
    ...rows,
    h("div", {class: "check"}, logs, h("label", {for: "lg"}, "Also add what you log by hand in the Nanit app (diapers, bottles, nursing, pumping, naps)?")),
    h("p", {class: "muted small"}, "Log each one in one app, not both: Miara can't tell that a diaper logged twice is the same one."),
    errBox(), h("div", {class: "row"}, btn));
}

async function viewCheck() {
  show(...head(4, "Check, then start"), h("p", {class: "muted"}, "Reading the last two days from Nanit…"));
  const r = await api("/api/preview");
  if (!r.ok) return show(...head(4, "Check, then start"), h("p", {class: "err"}, r.data.error),
    h("div", {class: "row"}, h("button", {type: "button", onclick: viewCheck}, "Try again"),
      h("button", {type: "button", class: "plain", onclick: () => go(viewNanit)}, "Sign in to Nanit again")));
  const d = r.data;
  const list = d.entries.length ? h("ul", {class: "list"}, d.entries.map(e => {
    const end = e.ended_at ? ` to ${new Date(e.ended_at).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"})}` : ", still asleep";
    return h("li", {}, h("span", {}, e.detail), h("span", {}, when(e.started_at) + (e.kind === "sleep" ? end : "")));
  })) : h("p", {class: "muted"}, "Nothing new in Nanit from the last two days. New sleeps and logs will come in as they happen.");
  const btn = h("button", {type: "button"}, d.entries.length || d.readings ? "Add these and start" : "Start");
  btn.onclick = () => busy(btn, "Adding…", async () => {
    const r2 = await api("/api/start", {});
    if (!r2.ok) return setErr(r2.data.error);
    await next();
  });
  show(...head(4, "Check, then start", d.entries.length ? "This is what Nanit has for the last two days. Nothing has been sent to Miara yet." : null),
    list, d.unread.map(u => h("p", {class: "muted small"}, `Not added: ${u.count} Nanit ${u.label} entries this bridge can't read yet.`)),
    d.readings ? h("p", {class: "muted"}, "And the nursery's temperature and humidity.") : null,
    errBox(), h("div", {class: "row"}, btn));
}

function viewDone() {
  const s = S.status;
  const signedOut = s.last_error && /signed this bridge out|Not signed in/i.test(s.last_error);
  const sent = s.last_sent ? s.last_sent.saved + s.last_sent.updated : 0;
  const out = h("button", {type: "button", class: "plain"}, "Sign in to Nanit again");
  out.onclick = () => go(viewNanit);
  const dis = h("button", {type: "button", class: "plain"}, "Disconnect");
  dis.onclick = () => {
    if (!confirm("Delete the saved Nanit sign-in and Miara connection from this site and stop checking? What's already in Miara stays until you undo it there.")) return;
    busy(dis, "Disconnecting…", async () => {
      const r = await api("/api/disconnect", {});
      if (!r.ok) return setErr(r.data.error);
      await next();
    });
  };
  show(...head(0, "It's running", "Nanit's sleeps are being added to Miara."),
    h("dl", {}, h("div", {}, h("dt", {}, "Last check"), h("dd", {}, ago(s.last_check))),
      h("div", {}, h("dt", {}, "Last time it worked"), h("dd", {}, ago(s.last_ok))),
      s.last_sent ? h("div", {}, h("dt", {}, "Added or changed then"), h("dd", {}, String(sent))) : null),
    s.last_error ? h("p", {class: "err", role: "alert"}, "The last check didn't work: " + s.last_error) : null,
    signedOut ? h("p", {}, "Sign in to Nanit again to fix this.") : null,
    h("p", {class: "muted"}, "Next check within about 15 minutes. A wake can show up 15 to 30 minutes after it happens."),
    errBox(), h("div", {class: "row"}, out, dis),
    h("div", {class: "row"}, h("button", {type: "button", class: "link", onclick: async () => { await api("/api/logout", {}); await next(); }}, "Sign out of this page")));
}
load();
</script>
</body>
</html>
"""
