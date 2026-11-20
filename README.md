# nanit-bridge

Sends your Nanit camera's sleeps to [Miara](https://getmiara.com) as they happen, with the nursery's temperature and humidity.

- **Sleeps.** When Nanit sees the baby fall asleep, Miara shows a sleep that's still going. When Nanit sees them wake, the sleep gets its end. Once Nanit finishes its own record of that sleep, Miara takes Nanit's final times. It stays one entry the whole way through.
- **Room.** Temperature and humidity from the camera, every 10 minutes.
- **Not sent:** Nanit's sleep scores, video, clips, photos, or anything logged in the Nanit app. Diapers and bottles stay where you log them.

Built from the [miara-bridge](https://github.com/noahpresler/miara-bridge) template.

> **Unofficial.** Nanit has no public API. This uses the calls the Nanit app makes, as mapped by the [ha-nanit](https://github.com/wealthystudent/ha-nanit) project (it uses that project's `aionanit` library). Nanit can change them at any time, and this stops working until someone catches up. Nanit's terms may not allow it. Use it for your own account.

## What you need

- A Nanit camera with sleep tracking (Nanit Insights). Without it you get room readings only.
- A computer that's always on: a Mac, a Raspberry Pi, a home server.
- Python 3.10 or newer. The `python3` that comes with macOS is 3.9: get a newer one with `brew install python` or [uv](https://docs.astral.sh/uv/) (`uv venv -p 3.13 .venv && uv pip install -p .venv -r requirements.txt`).
- A Miara token: in the app, **Profile → Siri, assistants and connections → New connection**, with `read`, `write:events` and `write:observations`, limited to the child or children this is for.

## Set it up

```sh
git clone https://github.com/noahpresler/nanit-bridge.git
cd nanit-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**1. Try it with made-up data.** This sends an invented baby's sleeps to Miara's public sandbox family. Nothing of yours is read or sent.

```sh
.venv/bin/python bridge.py --sandbox --dry-run   # shows what it would send
.venv/bin/python bridge.py --sandbox             # sends it
```

**2. Sign in to Nanit.**

```sh
.venv/bin/python bridge.py setup
```

It asks for your Nanit email, password and the code Nanit sends you (use the newest one; they expire quickly). The password is used for this sign-in and never saved. What's saved is the session Nanit gives back, in `~/.config/nanit-bridge/session.json`, readable only by you. That session is full access to your Nanit account: don't copy it anywhere. If you think it leaked, change your Nanit password (that ends every session) and run `setup` again.

**3. Connect your baby to Miara.** In the Miara app, make a token (**Profile → Siri, assistants and connections → New connection**, with `read`, `write:events` and `write:observations`, limited to your child). Then:

```sh
cp .env.example .env        # paste the token after MIARA_TOKEN=
.venv/bin/python bridge.py link
```

`link` pairs each Nanit baby with the Miara child of the same first name (or the only one) and saves it in `.env`. If it can't tell, it lists both sides so you can write `MIARA_CHILDREN=<Nanit uid>=<Miara child id>` yourself.

**4. See what it would send, then send it.**

```sh
.venv/bin/python bridge.py --dry-run
.venv/bin/python bridge.py
```

The bridge reads `.env` itself. The first run sends the last two days.

## Keep it running

`bridge.py watch` stays running and checks Nanit:

| When | How often |
|---|---|
| A sleep is going on | every minute, so the wake shows up quickly |
| Something happened in the last two hours | every 2 minutes |
| Quiet | every 10 minutes |
| Someone opens Miara | right away |

Each check reads the last two days and sends only what changed, so most checks send nothing.

- **Mac:** `schedule/com.example.nanit-bridge.plist` (instructions inside). It starts at login and restarts if it stops.
- **Linux:** `schedule/nanit-bridge.service`.

For a plain schedule instead (cron every few minutes): `.venv/bin/python bridge.py`.

## How it decides what a sleep is

- A sleep starts at Nanit's **fell asleep** (or **put down asleep**) and ends at its **woke up** (or **taken out asleep**).
- A wake shorter than 5 minutes doesn't end the sleep, in the live entry or in Nanit's finished record, which splits a night at every wake.
- When Nanit's own finished record arrives, usually soon after the wake, its times replace the live ones on the same entry.
- If someone already logged that sleep in Miara, the bridge's version doesn't become a second entry. Miara asks them whether to use Nanit's times.
- A sleep whose wake Nanit never reported is treated as over after 16 hours, and Nanit's finished record corrects it when it arrives.

## Several children

List each baby: `MIARA_CHILDREN=uidA=childA,uidB=childB`. One token can write to all of them if it's limited to those children.

## When something goes wrong

| You see | Do this |
|---|---|
| `Not signed in to Nanit` or `Nanit signed this bridge out` | Run `python bridge.py setup` again. |
| `Nanit answered 4xx/5xx for /babies/…` | Nanit changed something or is down. The bridge keeps trying. Check [ha-nanit's issues](https://github.com/wealthystudent/ha-nanit/issues) for news. |
| `No Miara child` | Set `MIARA_CHILDREN`. |
| `Daily limit reached` | It waits and carries on. Nothing is lost: the next check sends the rest. |
| No room readings | The camera is off or in sleep mode. Setting `NANIT_CAMERA_IP` (the camera's address on your network) can help. |

`python bridge.py probe` prints the shape of what Nanit returns: field names and event types, with no times, names or values. Paste it into an issue if Nanit's answers change.

To take back everything this bridge wrote: in Miara, open the connection and tap **Undo everything it added**.

## Tests

```sh
.venv/bin/python -m unittest                                       # offline
MIARA_LIVE=1 .venv/bin/python -m unittest tests.test_live_sandbox  # against the public sandbox
```

## License

MIT, like the template. `aionanit` is MIT too.
