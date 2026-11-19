# miara-bridge

A small script that sends your own device's data to Miara.

A **bridge** runs on your machine. It asks a device or service for what's new, using your own login for it, turns the answer into Miara entries (a sleep, a bottle, a diaper) and readings (room temperature, sleep stages), and sends them to your family's log. Miara keeps them as your family's data, shows them under the connection's name, and you can turn the connection off or undo everything it wrote.

This repo is the template. The part that's yours is one function, `fetch`. The rest is done: batching, retries, rate limits, idempotency, a summary of what happened.

- Python 3.10 or newer, and one dependency, `requests`.
- The API: <https://getmiara.com/developers> (plain-text version for agents: <https://getmiara.com/developers/llms.txt>).

## Try it in five minutes

The sandbox is a made-up family that anyone can read and write. It resets every night. You don't need an account.

```sh
git clone https://github.com/noahpresler/miara-bridge.git
cd miara-bridge
pip install -r requirements.txt

python bridge.py --sandbox --dry-run   # shows what it would send, sends nothing
python bridge.py --sandbox             # sends it
python bridge.py --sandbox             # again: nothing new, so nothing sent
```

The first real run looks like this:

```
Fetched 16 item(s) since the beginning: 5 entries, 11 readings.
Entries:  5 sent. 4 saved, 0 updated, 0 unchanged, 1 suggested, 0 skipped, 0 rejected
Readings: 11 sent, 11 new or changed
Declared metric x.sample.score
  suggested: sample:sleep-20261117: overlaps a sleep someone logged; it waits for them to answer
```

The sample data is invented by `fetch()` in `bridge.py`. It only runs against the sandbox: until you replace it, the script refuses to send to a real family.

## Make it yours

Open `bridge.py`. There are two functions to write and one list to edit.

**1. `fetch(since)`** gets what's new from your device or service and returns it as plain dicts. How depends on what you have: the service's API, an export file it gives you, a local database. Keep each item's own id.

**2. `map(items)`** turns those dicts into two lists.

- **Entries** are things that happened. `kind` is `sleep`, `bottle`, `nursing`, `diaper` or `pump`. Each needs a `source_event_id` built with `event_id()` from the service's own id, so the same item always lands on the same entry.
- **Readings** are measurements: `metric`, `started_at`, `value` (and `ended_at` for `sleep_stage`).

**3. `DECLARED_METRICS`** if your device reports something that isn't a core metric. Core metrics are `heart_rate`, `spo2`, `breathing_rate`, `room_temp_c`, `humidity`, `sound_db` and `sleep_stage`. Anything else is a metric of your own, named `x.<source>.<name>`, with a number range or 2 to 12 lowercase choices. The bridge declares it on its first run. Values are numbers or choices. Never free text.

Then set `USING_SAMPLE_DATA = False`, and test the same way as before:

```sh
python bridge.py --sandbox --dry-run
python bridge.py --sandbox
```

### Your device login

It stays on your machine. Put it in the environment your bridge runs in, for example a `.env` file next to `bridge.py`:

```
MIARA_TOKEN=miara_pat_...
MIARA_CHILD_ID=...
DEVICE_USER=...
DEVICE_PASSWORD=...
```

`.env` is in `.gitignore`. Your fetch code reads `os.environ["DEVICE_PASSWORD"]`. The bridge never sends it to Miara, and neither should your code: not in `raw`, not anywhere. Miara never asks for your device login or your Miara password.

### Your real family

1. In the Miara app: Profile → Siri, assistants and connections → New connection. Give it a name, pick the children and the scopes (`write:events` for entries, `write:observations` for readings). The token is shown once.
2. Put it in `.env` as `MIARA_TOKEN`. Put the child's id in `MIARA_CHILD_ID` (or pass `--child`). `GET /children` lists the ids.
3. `python bridge.py --dry-run`, then `python bridge.py`.

Settings, in the environment:

| Name | What it does |
|---|---|
| `MIARA_TOKEN` | Your token. |
| `MIARA_CHILD_ID` | The child to write to. `--child` does the same. |
| `MIARA_BASE` | The API address. Defaults to `https://getmiara.com/api/v1`. |

Flags: `--sandbox` (the public token and the sandbox's first child), `--dry-run`, `--since <time>`, `--full` (ignore the last sync and fetch everything), `--state <file>`.

## Schedule it

`state.json` remembers when the last run finished, so each run asks `fetch` only for what's new. Run it every 15 minutes to an hour; nothing breaks if a run is late or is skipped.

| Where | File |
|---|---|
| cron (Linux, macOS) | `schedule/crontab.example` |
| launchd (macOS) | `schedule/com.example.miara-bridge.plist` |
| systemd (Linux) | `schedule/miara-bridge.service` and `.timer` |
| GitHub Actions | `.github/workflows/bridge.yml` |

The Action is optional, and it has a cost. A bridge is meant to run where your device login already lives. On GitHub, that login has to be a repository secret, so GitHub, and anyone who can change the workflow, can use it. Keep the repo private, use a login that can only read, and limit the Miara token to one child.

## The rules

- **Every write is idempotent.** Each request carries an `Idempotency-Key` made from its content. Send the same batch again within 24 hours and Miara returns the first answer and saves nothing twice. Entries also carry your `source_event_id`: sending one again updates that entry instead of adding another. Readings are kept to the minute, one per child, metric and minute; a resend replaces it.
- **Overlaps come back `suggested`.** If a device's sleep overlaps one a person logged, it isn't saved twice. It waits for them to answer. If they answer or delete it, a resend comes back `skipped`. Don't work around either.
- **Vitals are records.** Heart rate, oxygen and breathing rate are kept and charted with real axes. Miara never interprets them, and neither should your bridge: no "normal", no "concerning", no advice.
- **Numbers and choices only.** No free text, no notes, nothing a person wrote.
- **Be gentle.** 120 requests a minute per token, 500 rows and 4 MB a request. On a 429 the bridge waits `Retry-After` and resends. On a daily limit it stops and tells you; the next run picks up where it left off. On a 5xx it backs off and tries again, up to five times.
- **Everything is the family's.** Anything you send shows in the log "via" your connection's name. They can undo all of it in one tap.

## What the summary means

| Word | Meaning |
|---|---|
| saved | New. |
| updated | You sent a change to something you sent before. |
| unchanged | Already there, same content. |
| suggested | Overlaps a sleep a person logged. Waits for them. |
| skipped | A person deleted it or answered its suggestion. Not brought back. |
| rejected | Miara refused it. The reason is printed. |

## Tests

```sh
python -m unittest discover -s tests -t .              # no network
MIARA_LIVE=1 python -m unittest tests.test_live_sandbox   # opt-in, uses the sandbox
```

## With an agent

`skills/miara-bridge/SKILL.md` is a skill that walks a coding agent through all of this: asking where your data comes from, writing `fetch` and `map`, testing on the sandbox, and scheduling. Copy the folder into your agent's skills directory.

## License

MIT. © 2026 Noah Presler.
