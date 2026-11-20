# nanit-bridge

Sends your Nanit camera's sleeps to [Miara](https://getmiara.com) as they happen, with the nursery's temperature and humidity.

- **Sleeps.** When Nanit sees the baby fall asleep, Miara shows a sleep that's still going. When Nanit sees them wake, the sleep gets its end. Once Nanit finishes its own record of that sleep, Miara takes Nanit's final times. It stays one entry the whole way through.
- **Room.** Temperature and humidity from the camera, every 10 minutes.
- **Not sent:** Nanit's sleep scores, video, clips, photos, or anything logged in the Nanit app. Diapers and bottles stay where you log them.

Built from the [miara-bridge](https://github.com/noahpresler/miara-bridge) template.

> **Unofficial.** Nanit has no public API. This uses the calls the Nanit app makes, as mapped by the [ha-nanit](https://github.com/wealthystudent/ha-nanit) project (it uses that project's `aionanit` library). Nanit can change them at any time, and this stops working until someone catches up. Nanit's terms may not allow it. Use it for your own account.

## What you need

- A Nanit camera with sleep tracking (Nanit Insights). Without it you get room readings only.
- A computer that stays on: a Mac, a Raspberry Pi, a home server.
- The Miara app, signed in to your family.

## Set it up

```sh
git clone https://github.com/noahpresler/nanit-bridge.git
cd nanit-bridge
./install.sh
```

It gets Python ready if this computer needs it, then walks you through four steps:

1. **Sign in to Nanit.** Email, password, and the code Nanit sends you. The password is used once and never saved. What's saved is the sign-in Nanit gives back, in `~/.config/nanit-bridge/session.json`, readable only by you. That sign-in is full access to your Nanit account, so don't copy it anywhere. If you think it leaked, change your Nanit password and run `./install.sh` again.
2. **Connect to Miara.** It tells you where to make a token in the app, then you paste it.
3. **Choose the child.** It matches Nanit's baby to the Miara child of the same name, or asks.
4. **Check, then start.** It shows the sleeps from the last two days and asks before adding them. Then it asks whether to keep running in the background (on a Mac it starts again at login).

Run `./install.sh` again any time to change something. It keeps what's done and asks before changing it.

To try it first with made-up data, sending an invented baby's sleeps to Miara's public sandbox family: `.venv/bin/python bridge.py --sandbox --dry-run`.

## Once it's running

| When | It checks Nanit |
|---|---|
| A sleep is going on | every minute, so the wake shows up quickly |
| Something happened in the last two hours | every 2 minutes |
| Quiet | every 10 minutes |
| Someone opens Miara | right away |

Each check reads the last two days and sends only what changed.

```sh
.venv/bin/python bridge.py status   # running? when did it last check?
.venv/bin/python bridge.py stop     # stop it, and don't start at login
```

On Linux, `./install.sh` sets everything up except the background part: use `schedule/nanit-bridge.service`.

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
| `Not signed in to Nanit` or `Nanit signed this bridge out` | Run `./install.sh` again. |
| `Nanit answered 4xx/5xx for /babies/…` | Nanit changed something or is down. The bridge keeps trying. Check [ha-nanit's issues](https://github.com/wealthystudent/ha-nanit/issues) for news. |
| `doesn't know which Miara child` | Run `./install.sh` again. |
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
