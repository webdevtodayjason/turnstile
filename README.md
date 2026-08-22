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

**POSIX only.** It is built on `fcntl.flock`, so Linux and macOS work and Windows does not
— the import fails outright rather than pretending. And one honest exception to the
crash-safety claim below: a `fork()` without `exec` passes the descriptor to the child, so
the child is reset explicitly (`os.register_at_fork`) rather than being allowed to inherit
a lock it does not own.

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

**And it is per-host.** `fcntl.flock` coordinates processes on one machine. If two
applications on *different* machines point at the same Tiiny, they share no lock file and
get no mutual exclusion whatsoever — "every call goes through Turnstile" reads like a
guarantee, and across machines it is not one. Same-host neighbours only. If you need
cross-machine coordination you need a broker, and you should write one knowing you are
also signing up to keep it alive.

---

## What it handles for you

**Both flavours of "busy".** Device code `150004` is the obvious one. HTTP `502` is the
one that wastes an afternoon: `/start` returns `200` immediately and then loads the model
asynchronously, so a call issued right after start fails for roughly twelve seconds while
the runtime reallocates. Both are retried with exponential backoff and jitter. Everything
else is raised, because retrying a genuine mistake just makes you wrong more slowly.

**Waiting politely.** A retrying caller releases the lock before each backoff sleep, so it
never sits on the device it is waiting for. Two deliberate exceptions:

* **Inside `hold()`** the sleep happens still holding, because that is what `hold()` means.
  The cost is real — one spurious 502 can park the device for the whole ladder, roughly a
  minute at defaults. Keep holds short.
* **After a read timeout** the lock is kept on purpose. Giving up on the socket does not
  stop the device, so our request is probably still running; letting go there hands the
  device to a peer who then collides with our own in-flight inference. Turnstile waits
  once (`settle_s`, default 60s) rather than re-issuing, because retrying a request that
  is still running *is* the collision.

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
| `TURNSTILE_DIR` | `/tmp` | where the lock file lives |

Two processes only coordinate if they lock the **same file**, so the default is `/tmp` and
the lock file is created mode `0666`. Both of those are deliberate, and both were bugs
first:

* `tempfile.gettempdir()` looks like the obvious default and is wrong. On macOS it is
  per-user (`/var/folders/…`), so an app running as a service account and an app running
  as you would take out two different lock files and coordinate with nobody — silently,
  which is the worst way for a lock to fail.
* The umask creates files `0644`, so the second user to arrive got `PermissionError`
  instead of a lock.

Running as one user on Linux, neither would ever have shown up. Set `TURNSTILE_DIR` to a
shared path if your processes are in containers that do not share `/tmp`.

The lock is per-device, so two Tiinys never block each other. Identity is the *resolved*
address, so `localhost` and `127.0.0.1` are correctly the same device — keying on the
spelling meant two apps could each think they were serialised while neither ever blocked.
Pass `lock_key="..."` to force two spellings together, or apart.

The port is deliberately not part of the identity: one device exposes several ports that
all contend for the same NPU, so they must share a lock. The known cost is that two
*different* Tiinys reached through tunnels on `127.0.0.1:8800` and `:8801` will queue
behind each other for no reason. Give them distinct `lock_key`s if that is your setup.

Lock files live in a sticky `turnstile/` subdirectory and their timestamp is refreshed on
every acquire, which keeps age-based tmp cleaners away from an actively used lock. That is
a mitigation, not a guarantee: for a long-lived service, point `TURNSTILE_DIR` at a
directory no cleaner touches.

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

Peak concurrency at the device was 1. Thirty checks in total: the retry path, giving up
honestly instead of hanging forever, `hold()` excluding an outsider, budget accounting,
binary responses surviving as bytes, a non-owner release being refused *before* the file
is unlocked, the lock file being usable by a second user — and the one that matters most,
**`kill -9` on the holder frees the device immediately.**

To validate against a real device, on the machine that can reach it:

```bash
sudo ./verify-on-device.sh
```

It reads the device key from `/etc/warboard.env` into its own environment, never prints
it, lists residency, makes one real inference, and then runs two concurrent processes
while whatever else you have running keeps using the device — real contention from a
workload that has never heard of the lock, which is the evidence that counts.

### Measured on hardware

Run on a **Tiiny AI Pocket** (firmware 0.1.33) over the USB link from an **Orange Pi 6 Plus**
(Ubuntu 26.04), while a 24/7 news board was independently using the same device:

```
residency        Ornith-35B 50u + Z-Image-Turbo 32u + Embedding 1u + Reranker 2u  (85/100)
one inference    deepreinforce-ai/Ornith-1.0-35B -> 'turnstile ok' in 8.6s
two processes    6/6 calls completed, 0 refused, 48.3s
the other app    items 1471, errors_24h 0, pending 0 — unaffected
```

The third line is the one that matters. Two processes competed for the device while a
third application that has never heard of Turnstile hammered it independently, and not one
call was refused. The NPU figures also agree with what the news board reports separately,
so the budget accounting is right against hardware and not just against the fake.

One honest note on how that run went: the first attempt failed with six consecutive `503`s.
The library was blameless — the verification script had picked the first resident model
over 20 units, which was the *image* model, and asked it to hold a conversation. The device
was right to refuse. Fixed in `verify-on-device.sh`, and a good reminder that "biggest
resident model" is not a synonym for "chat model".

---

## Watching it

The lock file carries a record of whoever holds it, written under the lock so the record
and the lock can never disagree. `who()` reads it **without taking the lock**, so a
dashboard can poll as often as it likes and never delay a real inference:

```python
import turnstile
turnstile.who()
```

```python
{"held": True, "owner": "warboard", "pid": 8412, "why": "enrich #1471",
 "held_for_s": 4.2, "queue": 2,
 "waiting": [{"owner": "story-lantern", "pid": 8477, "for_s": 1.4},
             {"owner": "reverie",       "pid": 8476, "for_s": 1.4}]}
```

Label your application when you build it, and say what a `hold()` is for:

```python
t = Turnstile(owner="reverie")          # or set TURNSTILE_OWNER
with t.hold("painting dream #412"):
    ...
```

Waiters register themselves only once they are genuinely blocked, so an uncontended call
costs nothing extra. A stale record from a crashed holder is harmless: readers only trust
it while the file is actually locked.

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
[WARBOARD](https://warboard.semfreak.dev) and Story Lantern, which taught it to us the
hard way.

*This repository is private while an NDA with the vendor is in force (to 2026-09-30).*

By [Jason Brashear](https://github.com/webdevtodayjason). Standard library only.
