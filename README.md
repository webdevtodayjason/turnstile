# Turnstile

**One Tiiny, two applications, no collisions.**

A Tiiny Pocket runs one inference at a time. Ask it for a second while the first is still
going and you get:

```json
{"code": 150004, "message": "The operation failed to complete."}
```

Inside one program that is easy — queue your own calls and move on. It stops being easy
the moment a second program touches the same device, because your queue and its queue
have never heard of each other. Two applications that are each perfectly well-behaved on
their own will still collide constantly.

We hit this running a 24/7 news board and a bedtime storyteller against the same device.
This is that lesson extracted, cleaned up, and handed over so nobody else has to
rediscover it at two in the morning.

```python
from turnstile import Turnstile

t = Turnstile()          # reads TIINY_HOST and TIINY_KEY

reply = t.chat("deepreinforce-ai/Ornith-1.0-35B",
               [{"role": "user", "content": "Say hello"}])
```

That is the whole API. Every call blocks until the device is free, retries the failures
that mean "busy", and gets out of the way.

---

## Install

Copy `turnstile.py` next to your code. That is the install.

Python 3.8+, standard library only. No pip, no daemon, no config file, no service to
supervise.

---

## Why a file lock

The coordination has to work **between processes**, so it cannot live in your program's
memory. The options were a broker daemon, a lock table in a database, or an advisory file
lock. We took the file lock because of what happens when an application crashes:

| | crash behaviour |
|---|---|
| broker daemon | needs supervision, and now you have two things that can die |
| lock row in a DB | stale lock, needs a heartbeat and a reaper |
| **`fcntl.flock`** | **the kernel releases it. Nothing to clean up.** |

If the process holding the device is killed with `-9`, the next caller gets in
immediately. There is no lease to expire, no heartbeat to miss, and no stuck state that
requires someone to log in and delete a file. That property is worth more than anything a
fancier design would have bought.

The trade is that the lock is *advisory*: a program that ignores Turnstile still collides.
That is a social problem, not a technical one, and every alternative has the same hole.

---

## What it handles for you

**Both flavours of "busy".** Device code `150004` is the obvious one. HTTP `502` is the
one that wastes an afternoon: `/start` returns `200` immediately and then loads the model
asynchronously, so a call issued right after start fails for roughly twelve seconds while
the runtime reallocates. Both are retried with exponential backoff and jitter. Everything
else is raised, because retrying a genuine mistake just makes you wrong more slowly.

**Waiting politely.** The lock is released before each backoff sleep, so a retrying caller
never sits on the device it is waiting for.

**The Ornith reasoning quirk.** The chat model puts its chain of thought in
`reasoning_content`, and that **counts against `max_tokens`**. Ask for a small budget and
`content` comes back empty with `finish_reason: length`, which looks like a bug in your
code and is not. `chat()` falls back to the reasoning text rather than handing you a
mysterious blank string.

**Sequences that must not be split.** `hold()` keeps the device across several calls:

```python
with t.hold("render a page"):
    text = t.chat(MODEL, [...])
    art  = t.image("Tongyi-MAI/Z-Image-Turbo", text[:200], seed=42)
```

Keep holds short. Everyone else is queued behind you.

**The 100-unit budget.** Optional, and worth using if you share the device:

```python
if t.budget.fits(32):
    t.start_model("Tongyi-MAI/Z-Image-Turbo")     # waits until actually running
```

`borrowed()` loads a model for a piece of work and gives the units back after — but only
if it was not already resident when you arrived. If somebody else's model is loaded, it
stays loaded. This is how a transient model coexists with another application's permanent
set instead of quietly evicting it mid-sentence.

```python
with t.borrowed("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", units=7):
    audio = t.speak("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "Good evening.")
```

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TIINY_HOST` | `127.0.0.1` | device address |
| `TIINY_KEY` | — | device API key |
| `TURNSTILE_DIR` | system temp | where the lock file lives |

Set `TURNSTILE_DIR` to a path both applications can see if they run in containers or under
different users — two processes only coordinate if they lock the *same file*. The lock is
per-device, so two Tiinys never block each other.

Constructor arguments cover the rest: `tries`, `base_delay`, `max_delay`, `timeout`, and
`on_wait`, a callback so your UI can say "device busy" instead of going silent.

---

## Tests

```bash
python3 tests/fake_device_test.py
```

A fake Tiiny that enforces the real contention rule — one call at a time, `150004` to
anything that overlaps — with real worker **processes** pointed at it, because in-process
queuing is the easy half and testing only that would prove nothing.

The suite opens with a **control** that runs the same load with no coordination. If the
control does not collide, the harness is not measuring anything and the run says so.

Measured on an Orange Pi 6 Plus (Ubuntu 26.04, Python 3.13), 4 processes × 6 calls:

| | collisions at the device | calls refused |
|---|---|---|
| control, no coordination | 18 | 18 of 24 |
| **through the turnstile** | **0** | **0 of 24** |

Peak concurrency at the device was 1. The suite also covers the retry path, giving up
honestly instead of hanging forever, `hold()` excluding an outsider, budget accounting,
and the one that matters most: **`kill -9` on the holder frees the device immediately.**

> **Status: validated against the fake device, not yet against hardware.** The contention
> contract the fake enforces, and the device behaviours documented above (`150004`,
> the ~12s 502 window after `/start`, the `reasoning_content` budget quirk, 512×512-only
> image generation) were all measured on a real Tiiny Pocket during the two applications
> this pattern came from. The library reproducing them end-to-end on hardware is a run
> that still needs doing — it needs a device API key. Do not take the table above as a
> hardware result; it is a fake-device result on the machine named.

---

## Worked example

```bash
python3 examples/two_apps.py --fake          # no hardware needed
python3 examples/two_apps.py --fake --chaos  # the same two apps, no turnstile
```

Two separate processes that have never heard of each other — a ticker doing short frequent
calls, a storyteller doing long occasional ones — sharing one device. Run it with
`--chaos` to watch the version people write first drop calls on the floor:

```
   0.07s  ticker    REFUSED  call 1 — device was busy (150004)
```

Drop `--fake` to run it against your own device with `TIINY_HOST` and `TIINY_KEY` set.

---

## What this is not

Not a daemon, not a scheduler, not a priority system, not a connection pool. It is a
turnstile: one at a time, first come first served, nobody gets stuck. That is deliberately
the smallest thing that solves the problem, and the smallest thing is what you want
sitting in the path of every call your application makes.

If you need priorities or fairness guarantees, you need a broker, and you should write one
knowing that you are also signing up to keep it alive.

---

## Credits

Built on the [Tiiny Pocket](https://tiiny.ai/) by Tiiny AI. Extracted from
[WARBOARD](https://warboard.semfreak.dev) and
[Story Lantern](https://github.com/webdevtodayjason/story-lantern), which taught it to us
the hard way.

By [Jason Brashear](https://github.com/webdevtodayjason). Standard library only.
