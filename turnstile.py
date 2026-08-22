#!/usr/bin/env python3
"""Turnstile — let two applications share one Tiiny without fighting over it.

    pip install nothing.  Copy this file next to yours.

WHY THIS EXISTS
---------------
A Tiiny Pocket performs ONE inference at a time. Issue a second call while another
is in flight and the device answers:

    {"code": 150004, "message": "The operation failed to complete."}

Inside a single program that is easy — queue your own calls. It stops being easy the
moment a second program touches the same device, because your queue and its queue know
nothing about each other. Two well-behaved applications, each perfectly serialised on
its own, will still collide constantly.

We learned this running a 24/7 news board and a bedtime storyteller against one device.
This module is that lesson, extracted and cleaned up so nobody else has to rediscover it.

WHAT IT DOES
------------
* Serialises every device call across PROCESSES, not just threads, using an advisory
  file lock. If a holder crashes, the kernel releases the lock — no stale-lock cleanup,
  no heartbeat, no daemon to run.
* Retries the two failures that mean "busy, try again" — device code 150004 and HTTP
  502/503/504 — with exponential backoff and jitter.
* Optionally tracks the 100-unit model residency budget in a small shared ledger, so an
  application can ask "will my model fit?" before it evicts somebody else's.

WHAT IT DOES NOT DO
-------------------
It is not a daemon, a scheduler, or a priority system. It is a turnstile: one at a time,
first come first served, nobody gets stuck. That is deliberately the smallest thing that
solves the actual problem.

USAGE
-----
    from turnstile import Turnstile

    t = Turnstile()                       # reads TIINY_HOST / TIINY_KEY

    reply = t.chat("deepreinforce-ai/Ornith-1.0-35B",
                   [{"role": "user", "content": "Say hello"}])

    png = t.image("Tongyi-MAI/Z-Image-Turbo", "a lighthouse at dusk", seed=7)

    with t.hold("rendering a page", seconds=120):   # several calls, no interleaving
        a = t.chat(...)
        b = t.image(...)

Everything blocks until the device is free. That is the point.
"""

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import random
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request

__all__ = ["Turnstile", "DeviceBusy", "DeviceError", "Budget"]

DEFAULT_PORT = 8800
NPU_TOTAL = 100

# The two responses that mean "the device is busy", as opposed to "you asked for
# something impossible". 150004 is the device's own concurrency error. 502/503/504 is
# the same condition seen through the gateway — notably, a model that is still loading
# produces 502 for roughly twelve seconds after /start returns 200.
BUSY_DEVICE_CODE = 150004
BUSY_HTTP = (502, 503, 504)

# Where the cross-process lock lives. One file per device, so two Tiinys don't block
# each other. Override with TURNSTILE_DIR if /tmp is not shared between your processes.
LOCK_DIR = os.environ.get("TURNSTILE_DIR") or tempfile.gettempdir()


class DeviceBusy(RuntimeError):
    """The device stayed busy for the whole retry budget."""


class DeviceError(RuntimeError):
    """The device refused the request for a reason retrying will not fix."""


# --------------------------------------------------------------------------- #
# the lock
# --------------------------------------------------------------------------- #

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path):
    """One lock object per lock file per process — and it has to be exactly one.

    fcntl.flock is per open file description, not per process, so two lock objects in
    one program hold two descriptions and genuinely block each other. An application
    that builds a Turnstile in two different modules would then deadlock against
    itself, forever, with the default of waiting indefinitely. Sharing the object makes
    that case reentrant instead, which is what a caller means by it.
    """
    with _LOCKS_GUARD:
        got = _LOCKS.get(path)
        if got is None:
            got = _LOCKS[path] = _CrossProcessLock(path)
        return got


class _CrossProcessLock:
    """An advisory file lock, reentrant within a process.

    fcntl.flock is per-file-descriptor and released by the kernel when the process
    exits, which is exactly the ownership semantics we want: a crashed application
    must not wedge the device for everybody else. It does mean the lock is advisory —
    a program that ignores Turnstile still collides. That is a social problem, not a
    technical one.
    """

    def __init__(self, path):
        self.path = path
        self._fh = None
        self._depth = 0
        self._owner = None
        self._local = threading.RLock()

    def acquire(self, timeout=None):
        # Two gates: the in-process RLock keeps our own threads in line, the file lock
        # keeps us in line with everybody else. The timeout has to cover both or a
        # caller that asked to wait 5s can block forever behind a sibling thread.
        deadline = None if timeout is None else time.time() + timeout
        if not self._local.acquire(timeout=-1 if timeout is None else max(0.0, timeout)):
            return False
        if self._depth:                      # already ours; just nest
            self._depth += 1
            return True
        # Everything from here until _depth is set must hand the RLock back on the way
        # out, or one bad open() wedges this process for good.
        fh = None
        try:
            fh = open(self.path, "a+")
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fh = fh
                    self._depth = 1
                    self._owner = threading.get_ident()
                    fh = None                # owned by self now; don't close it below
                    return True
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if deadline is not None and time.time() >= deadline:
                        return False
                    time.sleep(0.05 + random.random() * 0.05)
        finally:
            if self._depth == 0:             # we are leaving without the lock
                if fh is not None:
                    fh.close()
                self._local.release()

    def release(self):
        if not self._depth:
            return
        # Refuse before touching anything. Without this, a thread releasing a lock it
        # does not hold would unlock the FILE first and only then hit the RLock's own
        # RuntimeError — handing the device to everyone else while the real owner is
        # still mid-inference and believes it holds the lock.
        if self._owner != threading.get_ident():
            raise RuntimeError("release() from a thread that does not hold the lock")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        self._local.release()

    @contextlib.contextmanager
    def held(self, timeout=None):
        if not self.acquire(timeout):
            raise DeviceBusy("could not take the device lock within %ss" % timeout)
        try:
            yield
        finally:
            self.release()


# --------------------------------------------------------------------------- #
# the residency ledger
# --------------------------------------------------------------------------- #

class Budget:
    """A shared view of the 100-unit model residency budget.

    The device is the source of truth; this is a cache plus a note of who asked for
    what, so an application can be polite instead of merely lucky. Ask `fits()` before
    loading a model and you stop being the app that evicts somebody's chat model in the
    middle of their sentence.
    """

    def __init__(self, turnstile, path=None):
        self.t = turnstile
        self.path = path or os.path.join(LOCK_DIR, "turnstile-%s.budget.json" % turnstile.key_id)

    def resident(self):
        """[(model_id, units, status)] straight from the device."""
        data = self.t.get("/api/v1/models/npu/status")
        return [(m.get("model_id"), int(m.get("npu_usage") or 0), m.get("status"))
                for m in (data.get("models") or [])]

    def used(self):
        return sum(u for _, u, s in self.resident() if s == "running")

    def free(self):
        return NPU_TOTAL - self.used()

    def fits(self, units):
        return self.free() >= int(units)

    def note(self, model_id, units, owner):
        """Record that `owner` wants `model_id` resident. Advisory, for humans."""
        try:
            with open(self.path) as fh:
                book = json.load(fh)
        except Exception:
            book = {}
        book[str(model_id)] = {"units": int(units), "owner": str(owner), "ts": time.time()}
        # Unique temp name: two processes noting at once would otherwise write the same
        # file and one would rename a half-written copy over the other's.
        tmp = "%s.%d.tmp" % (self.path, os.getpid())
        with open(tmp, "w") as fh:
            json.dump(book, fh, indent=1)
        os.replace(tmp, self.path)
        return book


# --------------------------------------------------------------------------- #
# the turnstile
# --------------------------------------------------------------------------- #

class Turnstile:
    def __init__(self, host=None, key=None, port=DEFAULT_PORT,
                 tries=6, base_delay=2.0, max_delay=30.0, timeout=300.0,
                 on_wait=None):
        self.host = host or os.environ.get("TIINY_HOST") or "127.0.0.1"
        self.key = key or os.environ.get("TIINY_KEY") or ""
        self.base = "http://%s:%d" % (self.host, int(port))
        self.tries = int(tries)
        self.base_delay = float(base_delay)
        self.max_delay = float(max_delay)
        self.timeout = float(timeout)
        # on_wait(attempt, delay, why) — hook so an application can say "device busy"
        # in its own UI instead of going silent.
        self.on_wait = on_wait
        # Identifies the lock FILE, so it has to be one-to-one with the device. Keeping
        # the readable host in the name helps when someone is staring at a temp dir, but
        # stripping the dots alone collides (172.17.7.177 and 17.21.77.177 both become
        # 172177177) and would make two separate devices queue behind each other.
        # The port is deliberately NOT included: one Tiiny exposes several ports and
        # they all contend for the same NPU, so they must share a lock.
        safe = "".join(c if c.isalnum() else "-" for c in self.host).strip("-") or "device"
        self.key_id = "%s-%s" % (safe[:32], hashlib.sha1(self.host.encode()).hexdigest()[:8])
        self._lock = _lock_for(os.path.join(LOCK_DIR, "turnstile-%s.lock" % self.key_id))
        self.budget = Budget(self)

    # -- plumbing ---------------------------------------------------------- #

    def _request(self, path, body=None, method=None, timeout=None, raw=False):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + self.key}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            blob = resp.read()
            ctype = resp.headers.get("Content-Type", "")
        if raw and "json" not in ctype:
            return blob
        try:
            return json.loads(blob or b"{}")
        except ValueError:
            return blob if raw else {}

    @staticmethod
    def _busy_reason(exc_or_payload):
        """Is this 'try again' or 'you are wrong'? Returns a reason string or None."""
        if isinstance(exc_or_payload, urllib.error.HTTPError):
            if exc_or_payload.code in BUSY_HTTP:
                return "http %d" % exc_or_payload.code
            return None
        if isinstance(exc_or_payload, (urllib.error.URLError, TimeoutError, OSError)):
            # A reset or a timeout mid-inference is indistinguishable from contention
            # from out here, and retrying is safe because inference has no side effects.
            # A name that does not resolve is different: it will still not resolve in
            # thirty seconds, so a typo in TIINY_HOST should fail now rather than after
            # the full backoff ladder.
            inner = getattr(exc_or_payload, "reason", exc_or_payload)
            if isinstance(inner, socket.gaierror):
                return None
            return "transport: %s" % str(exc_or_payload)[:60]
        if isinstance(exc_or_payload, dict):
            code = exc_or_payload.get("code") or (exc_or_payload.get("error") or {}).get("code")
            try:
                if int(code) == BUSY_DEVICE_CODE:
                    return "device %d" % BUSY_DEVICE_CODE
            except (TypeError, ValueError):
                pass
        if isinstance(exc_or_payload, (bytes, bytearray)) and exc_or_payload[:1] == b"{":
            try:
                return Turnstile._busy_reason(json.loads(exc_or_payload))
            except ValueError:
                return None
        return None

    def call(self, path, body=None, method="POST", raw=False, timeout=None):
        """One device call, serialised and retried. This is the whole library."""
        last = None
        for attempt in range(1, self.tries + 1):
            with self._lock.held(timeout=None):
                try:
                    out = self._request(path, body, method, timeout=timeout, raw=raw)
                    why = self._busy_reason(out)
                    if why is None:
                        if raw and not isinstance(out, (bytes, bytearray)):
                            # We promised bytes and got a JSON object that is not a busy
                            # signal, so it is the device declining. Say so rather than
                            # handing back a dict where a caller expects a PNG.
                            raise DeviceError("device declined: %s" % str(out)[:200])
                        return out
                    last = why
                except urllib.error.HTTPError as exc:
                    body_txt = b""
                    try:
                        body_txt = exc.read()[:400]
                    except Exception:
                        pass
                    why = self._busy_reason(exc) or self._busy_reason(body_txt)
                    if why is None:
                        raise DeviceError("HTTP %s: %s" % (exc.code, body_txt[:200].decode("utf-8", "replace")))
                    last = why
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    why = self._busy_reason(exc)
                    if why is None:
                        raise
                    last = why
            # released the lock before sleeping: whoever is actually using the device
            # should get it, not us.
            if attempt < self.tries:
                delay = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
                delay += random.random() * (delay * 0.25)
                if self.on_wait:
                    try:
                        self.on_wait(attempt, delay, last)
                    except Exception:
                        pass
                time.sleep(delay)
        raise DeviceBusy("device stayed busy across %d attempts (%s)" % (self.tries, last))

    def get(self, path, timeout=None):
        return self.call(path, None, method="GET", timeout=timeout)

    @contextlib.contextmanager
    def hold(self, why="", wait=None):
        """Hold the device across several calls so nothing interleaves.

        Use it when a sequence must not be broken up — writing a page and immediately
        illustrating it, say. Keep it short: everyone else is waiting on you.

        `wait` bounds how long we queue for our turn, not how long we may keep it.
        None waits forever; a number raises DeviceBusy if the door never opens.
        """
        with self._lock.held(timeout=wait):
            yield self

    # -- the calls people actually make ------------------------------------ #

    def chat(self, model, messages, max_tokens=800, temperature=0.2, **kw):
        """Chat completion. Returns the assistant text.

        NOTE the Ornith quirk this handles for you: the model puts its chain of thought
        in `reasoning_content`, and that COUNTS AGAINST max_tokens. Ask for a small
        budget and `content` comes back empty with finish_reason 'length'. We ask for a
        real budget and, if content is still empty, hand back the reasoning so the
        caller can salvage something rather than seeing a mysterious blank.
        """
        body = {"model": model, "messages": messages,
                "max_tokens": int(max_tokens), "temperature": temperature}
        body.update(kw)
        out = self.call("/v1/chat/completions", body)
        msg = ((out.get("choices") or [{}])[0].get("message") or {})
        return (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()

    def embed(self, model, text):
        out = self.call("/v1/embeddings", {"model": model, "input": text})
        return (out.get("data") or [{}])[0].get("embedding")

    def rerank(self, model, query, documents):
        out = self.call("/v1/rerank", {"model": model, "query": query, "documents": documents})
        return out.get("results") or []

    def image(self, model, prompt, seed=0, negative_prompt="", steps=8, size=512):
        """Text to image. Returns PNG bytes.

        `size` is 512 and you should leave it there: on current firmware every other
        dimension fails with 150004 after about thirty seconds, which looks exactly
        like contention and is not.
        """
        return self.call("/v1/image/generate", {
            "model": model, "prompt": prompt, "negative_prompt": negative_prompt,
            "width": int(size), "height": int(size), "seed": int(seed), "steps": int(steps),
        }, raw=True)

    def speak(self, model, text, fmt="mp3"):
        """Text to speech. Returns audio bytes.

        Send no `voice` field for the CustomVoice model — it needs none, and its Base
        sibling rejects every speaker name we have been able to find.
        """
        return self.call("/v1/audio/speech",
                         {"model": model, "input": text, "response_format": fmt}, raw=True)

    # -- model residency --------------------------------------------------- #

    def start_model(self, model_id, wait=True, timeout=180.0):
        """Load a model, and actually wait for it.

        /start returns 200 within a fraction of a second and then loads asynchronously.
        A call issued immediately after hits the runtime mid-reallocation and comes back
        502 for roughly twelve seconds. So we poll until the device says 'running'.
        """
        self.call("/api/v1/models/%s/start" % model_id.replace("/", "%2F"), {})
        if not wait:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            for mid, _units, status in self.budget.resident():
                if mid == model_id and status == "running":
                    return True
            time.sleep(3)
        return False

    def stop_model(self, model_id):
        self.call("/api/v1/models/%s/stop" % model_id.replace("/", "%2F"), {})

    @contextlib.contextmanager
    def borrowed(self, model_id, units=None):
        """Load a model for a piece of work and give the units back afterwards.

        If it was already resident when you arrived it is somebody else's and we leave
        it alone. This is how a transient model (speech, OCR) coexists with somebody
        else's permanent set instead of quietly stealing their budget.
        """
        mine = True
        for mid, _u, status in self.budget.resident():
            if mid == model_id and status == "running":
                mine = False
                break
        if mine:
            if units is not None and not self.budget.fits(units):
                raise DeviceError("only %du free; %s needs %du" % (self.budget.free(), model_id, units))
            self.start_model(model_id)
        try:
            yield self
        finally:
            if mine:
                try:
                    self.stop_model(model_id)
                except Exception:
                    pass


if __name__ == "__main__":
    import sys
    t = Turnstile(on_wait=lambda a, d, why: print("  waiting %.1fs (%s)" % (d, why)))
    print("device:", t.base)
    try:
        for mid, units, status in t.budget.resident():
            print("  %-42s %3du  %s" % (mid, units, status))
        print("  free: %du of %d" % (t.budget.free(), NPU_TOTAL))
    except Exception as exc:
        print("  could not reach the device:", str(exc)[:120])
        sys.exit(1)
    if "--chat" in sys.argv:
        models = [m for m, _u, s in t.budget.resident() if s == "running"]
        if models:
            print("  chat ->", t.chat(models[0], [{"role": "user", "content": "Say hello in five words."}],
                                      max_tokens=400)[:120])
