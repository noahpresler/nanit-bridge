---
name: miara-bridge
description: Build a bridge that sends a person's own device or service data (sleep, feeds, diapers, pumps, room readings, vitals) to their Miara log. Use when someone wants data from a baby monitor, tracker, sensor or export file in Miara, or asks to write, test or schedule a Miara bridge.
---

# Build a Miara bridge

You are helping someone send their own data to their family's Miara log with the `miara-bridge` template (this repo). The template already does batching, retries, rate limits, idempotency keys and the summary. Your job is two functions, `fetch` and `map`, plus testing and scheduling.

Reference: https://getmiara.com/developers/llms.txt (short) and https://getmiara.com/api/v1/openapi.json (full).

## Rules. Never break these.

1. **Never interpret vitals.** Heart rate, oxygen and breathing rate are records. Don't label them normal, high, low or concerning, don't suggest what to do, and don't build alerts from them.
2. **Never send free text.** Readings are numbers or one of a fixed set of lowercase choices. No notes, names, messages or anything a person typed.
3. **Never ask for the person's Miara password.** Miara access is a token they make in the app. Nothing else.
4. **The device login stays local.** It lives only in the person's own environment (a `.env` that is in `.gitignore`, or their system's secret store). Never put it in the repo, in `raw`, in a log, or in any request to Miara. Never print it.
5. **Use the sandbox until the person says go.** Don't send anything real before the sandbox run looks right, and never send real data to the sandbox.
6. **Don't work around `suggested` or `skipped`.** Those are people's answers.

## Steps

### 1. Find out what they have

Ask, one thing at a time:
- Which device or service is it, and which child is it for?
- How does its data come out? An official export (a file or a download), an API they can log in to, or a file or database on their own machine? Prefer the official way over anything that imitates the app.
- What does it record? Sleeps, feeds, diapers, pumps (entries), and room temperature, humidity, sound, sleep stages, heart rate, oxygen, breathing (readings), or something else?
- Where will it run? Their own computer or a small always-on machine is the default. GitHub Actions is possible but puts the device login in GitHub secrets; say so.

### 2. Write `fetch(since)`

In `bridge.py`. Return what's new since `since` (a UTC datetime, `None` on the first run) as plain dicts, each with the service's own id. Read the device login from `os.environ`, and add the variable names to the README's `.env` example (names only, no values). If the person pastes a password or token into the chat, tell them to put it in `.env` and not to share it again. Handle the service's paging and its own errors; if it fails, raise an exception with a plain message (the bridge prints it and doesn't move the last sync time).

### 3. Write `map(items)`

Turn items into entries and readings.

- **Entries**: kinds `sleep`, `bottle`, `nursing`, `diaper`, `pump` only. `source_event_id` always from `event_id(<the service's own id>)`, never from the run time, a counter or a hash of the content. Details follow the API: bottle `volume_ml` and `contents` (`breast_milk`, `formula`, `mixed`); nursing `left_seconds`, `right_seconds`; diaper `diaper` (`wet`, `dirty`, `mixed`, `dry`); pump `total_ml` or `duration_seconds`; sleep `{}` with `started_at` and `ended_at`. Convert units yourself. Times are ISO 8601 with an offset; `iso()` does it.
- **Readings**: first look at `GET /metrics`. If a **core** metric fits (`heart_rate`, `spo2`, `breathing_rate`, `room_temp_c`, `humidity`, `sound_db`, `sleep_stage`), use it exactly, in its unit and range. Otherwise declare an `x.<source>.<metric>` in `DECLARED_METRICS`: lowercase letters, digits and underscores, a short plain label, and either `min`/`max`/`unit` for a number or 2 to 12 lowercase `choices`. Never a text field. If the device's score or label can't be a number or a fixed choice, leave it out.
- Leave out anything you can't map cleanly. Print nothing about it that includes personal data.

Set `SOURCE` to a short lowercase name for this bridge, update the stub's `x.sample.score` declaration or delete it, and set `USING_SAMPLE_DATA = False`.

### 4. Test on the sandbox

```sh
python bridge.py --sandbox --dry-run   # check the printed entries and readings by eye
python bridge.py --sandbox             # sends to the made-up family
python bridge.py --sandbox             # should send nothing new
python -m unittest discover -s tests -t .
```

Read the summary with the person. `saved` and `unchanged` are good. `suggested` means a sleep overlapped one that was already there. `rejected` prints a reason; fix `map` and run again. The sandbox is shared and limited to 30 requests a minute across everyone, so don't loop.

Add a unit test for `map` with a couple of real-shaped sample items (with names and values changed or made up).

### 5. Make a token

Only when the sandbox run is right. Tell the person to open the Miara app: **Profile → Siri, assistants and connections → New connection**. Name it after the device, pick the child, and pick only the scopes the bridge needs (`write:events` for entries, `write:observations` for readings). The token starts with `miara_pat_` and is shown once. They put it in `.env` as `MIARA_TOKEN`, with the child's id as `MIARA_CHILD_ID`. Don't ask them to paste it to you.

Then `python bridge.py --dry-run` and `python bridge.py` against their real family, and check the log in the app shows the entries under the connection's name.

### 6. Schedule it

Pick the file in `schedule/` for where it runs: cron, launchd (macOS), a systemd timer (Linux), or `.github/workflows/bridge.yml` (and explain the trade-off: the device login becomes a GitHub secret). Every 15 minutes to an hour is plenty. Replace the example paths. Confirm one scheduled run worked by reading its log.

## When something goes wrong

- `token` or `scope` errors: wrong or expired token, or the scope or child wasn't ticked. They make a new connection in the app.
- `daily_limit`: stop. The next day's run picks up.
- Everything `rejected` for one reason: fix `map`, don't retry.
- Data looks wrong in the app: the person can turn the connection off or undo everything it wrote from the same settings screen. Fix and resend; `source_event_id` makes it update rather than duplicate.
