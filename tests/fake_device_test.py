#!/usr/bin/env python3
"""Prove the turnstile actually turns.

    python3 tests/fake_device_test.py

A fake Tiiny that behaves like the real one in the way that matters: it accepts one
inference at a time and answers 150004 to anything that overlaps. Then we point real
worker PROCESSES at it, because in-process queuing is the easy half of the problem and
testing only that would prove nothing.

The suite includes a CONTROL that hammers the fake device without a turnstile. If the
control does not collide, the test is not measuring anything and says so.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import turnstile as turnstile_mod
from turnstile import Turnstile, DeviceBusy, DeviceError, _CrossProcessLock  # noqa: E402

HOST, PORT = "127.0.0.1", 8899
WORK_S = 0.12          # how long a fake inference "takes"


class Device:
    """Counters shared by the handler threads."""
    def __init__(self):
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.collisions = 0
        self.served = 0
        self.fail_next = 0        # force N busy replies, to exercise the retry path
        self.decline_image = False

    def enter(self):
        with self.lock:
            if self.fail_next > 0:
                self.fail_next -= 1
                return False
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            if self.inflight > 1:
                self.collisions += 1
                self.inflight -= 1
                return False
            return True

    def leave(self):
        with self.lock:
            self.inflight -= 1
            self.served += 1


DEV = Device()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        blob = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        if self.path == "/stats":
            return self._send(200, {"peak": DEV.peak, "collisions": DEV.collisions,
                                    "served": DEV.served})
        if self.path == "/api/v1/models/npu/status":
            return self._send(200, {"models": [
                {"model_id": "fake/chat-35B", "npu_usage": 50, "status": "running"},
                {"model_id": "fake/tts", "npu_usage": 7, "status": "stopped"}]})
        self._send(404, {"error": "nope"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path.startswith("/api/v1/models/"):
            return self._send(200, {"ok": True})
        if self.path == "/v1/image/generate":
            # The real device answers with raw PNG bytes, or a JSON body when it
            # declines. Both shapes have to reach the caller correctly.
            if DEV.decline_image:
                return self._send(200, {"code": 400, "message": "unsupported size"})
            png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            return self.wfile.write(png)
        if not DEV.enter():
            # exactly what a real Tiiny says when it is already thinking
            return self._send(200, {"code": 150004,
                                    "message": "The operation failed to complete."})
        try:
            time.sleep(WORK_S)
        finally:
            DEV.leave()
        self._send(200, {"choices": [{"message": {"content": "ok", "reasoning_content": ""}}]})


def stats():
    with urllib.request.urlopen("http://%s:%d/stats" % (HOST, PORT), timeout=5) as r:
        return json.load(r)


# --------------------------------------------------------------------------- #
# the worker, re-entered as a subprocess
# --------------------------------------------------------------------------- #

def _worker():
    mode, calls = sys.argv[2], int(sys.argv[3])
    ok = err = 0
    if mode == "turnstile":
        t = Turnstile(host=HOST, port=PORT, key="x", tries=8, base_delay=0.05, max_delay=0.4)
        for _ in range(calls):
            try:
                t.chat("fake/chat-35B", [{"role": "user", "content": "hi"}])
                ok += 1
            except DeviceBusy:
                err += 1
    else:                                   # control: no coordination at all
        for _ in range(calls):
            req = urllib.request.Request(
                "http://%s:%d/v1/chat/completions" % (HOST, PORT),
                data=b'{"model":"fake/chat-35B"}',
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = json.load(r)
                err += 1 if body.get("code") == 150004 else 0
                ok += 0 if body.get("code") == 150004 else 1
            except urllib.error.URLError:
                err += 1
    print(json.dumps({"ok": ok, "err": err}))


def spawn(mode, procs, calls):
    me = os.path.abspath(__file__)
    running = [subprocess.Popen([sys.executable, me, "--worker", mode, str(calls)],
                                stdout=subprocess.PIPE, text=True) for _ in range(procs)]
    out = []
    for p in running:
        stdout, _ = p.communicate(timeout=180)
        out.append(json.loads(stdout.strip().splitlines()[-1]))
    return {"ok": sum(o["ok"] for o in out), "err": sum(o["err"] for o in out)}


# --------------------------------------------------------------------------- #

def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    failures = []

    def check(name, cond, detail=""):
        print("%-4s %s%s" % ("PASS" if cond else "FAIL", name, ("  " + detail) if detail else ""))
        if not cond:
            failures.append(name)

    # 1. CONTROL — no turnstile. This must collide, or the harness proves nothing.
    print("\n-- control: 4 processes, no coordination --")
    DEV.peak = DEV.collisions = DEV.served = 0
    c = spawn("control", 4, 6)
    s = stats()
    check("control collides (harness is real)", s["collisions"] > 0,
          "%d collisions, %d rejected calls" % (s["collisions"], c["err"]))

    # 2. THE CLAIM — same load, through the turnstile.
    print("\n-- turnstile: 4 processes x 6 calls --")
    DEV.peak = DEV.collisions = DEV.served = 0
    t0 = time.time()
    r = spawn("turnstile", 4, 6)
    el = time.time() - t0
    s = stats()
    check("no call was ever rejected", r["err"] == 0, "%d ok, %d failed" % (r["ok"], r["err"]))
    check("all 24 calls completed", r["ok"] == 24, "%d" % r["ok"])
    check("device never saw two at once", s["peak"] <= 1, "peak inflight %d" % s["peak"])
    check("zero collisions at the device", s["collisions"] == 0, "%d" % s["collisions"])
    check("throughput is serial, not stalled", el < 24 * WORK_S * 3.0,
          "%.1fs for 24 x %.2fs of work" % (el, WORK_S))

    # 3. Retry path: force the device to answer busy a few times.
    print("\n-- retry: device answers 150004 three times --")
    DEV.fail_next = 3
    waits = []
    t = Turnstile(host=HOST, port=PORT, key="x", tries=8, base_delay=0.05, max_delay=0.4,
                  on_wait=lambda a, d, why: waits.append(why))
    got = t.chat("fake/chat-35B", [{"role": "user", "content": "hi"}])
    check("survived transient busy", got == "ok", "%d retries, reasons %s" % (len(waits), set(waits)))
    check("recognised it as device 150004", all("150004" in w for w in waits), str(waits))

    # 4. Give up honestly rather than hanging forever.
    print("\n-- give up: device is busy for good --")
    DEV.fail_next = 999
    t2 = Turnstile(host=HOST, port=PORT, key="x", tries=3, base_delay=0.02, max_delay=0.05)
    try:
        t2.chat("fake/chat-35B", [{"role": "user", "content": "hi"}])
        check("raises DeviceBusy instead of hanging", False, "returned normally")
    except DeviceBusy as exc:
        check("raises DeviceBusy instead of hanging", True, str(exc)[:60])
    DEV.fail_next = 0

    # 5. hold() keeps a sequence together.
    print("\n-- hold: a multi-call sequence is not interleaved --")
    DEV.peak = DEV.collisions = 0
    intruder = {"got_in": None}

    def outsider():
        o = Turnstile(host=HOST, port=PORT, key="x", tries=1)
        time.sleep(0.05)
        intruder["got_in"] = o._lock.acquire(timeout=0.05)
        if intruder["got_in"]:
            o._lock.release()

    th = threading.Thread(target=outsider)
    with t.hold("a sequence"):
        th.start()
        t.chat("fake/chat-35B", [{"role": "user", "content": "1"}])
        t.chat("fake/chat-35B", [{"role": "user", "content": "2"}])
    th.join()
    check("outsider was kept out during hold", intruder["got_in"] is False)
    check("no collisions during hold", stats()["collisions"] == 0)

    # 6. A crashed holder must not wedge the device.
    print("\n-- crash: lock dies with the process that held it --")
    lockpath = t._lock.path
    kid = subprocess.Popen([sys.executable, "-c",
                            "import fcntl,sys,time\n"
                            "fh=open(%r,'a+')\n" % lockpath +
                            "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
                            "print('held', flush=True)\n"
                            "time.sleep(30)\n"], stdout=subprocess.PIPE, text=True)
    kid.stdout.readline()
    blocked = _CrossProcessLock(lockpath).acquire(timeout=0.3)
    check("a live holder blocks others", blocked is False)
    kid.kill()
    kid.wait()
    freed = _CrossProcessLock(lockpath)
    check("killing the holder frees the device", freed.acquire(timeout=2.0) is True)
    freed.release()

    # 7. Budget reads residency off the device.
    print("\n-- budget --")
    check("counts only running models", t.budget.used() == 50, "used %du" % t.budget.used())
    check("reports free units", t.budget.free() == 50)
    check("fits() is honest", t.budget.fits(32) and not t.budget.fits(60))

    # 8. Regressions from review.
    print("\n-- regressions --")
    # (a) An unopenable lock file must not strand the in-process RLock. Before the fix
    #     the failed acquire kept the RLock, and every later call in this process hung.
    bad = _CrossProcessLock("/nonexistent-dir-%d/lock" % os.getpid())
    try:
        bad.acquire(timeout=0.2)
    except OSError:
        pass
    check("failed acquire releases the in-process lock",
          bad._local.acquire(timeout=0.5) is True, "would hang forever if leaked")
    try:
        bad._local.release()
    except RuntimeError:
        pass

    # (b) A hostname that cannot resolve is not 'busy'. It must fail now, not after the
    #     whole backoff ladder.
    t3 = Turnstile(host="no-such-device.invalid", key="x", tries=6, base_delay=2.0)
    t0 = time.time()
    try:
        t3.chat("m", [{"role": "user", "content": "hi"}])
        check("unresolvable host fails fast", False, "returned normally")
    except DeviceBusy:
        check("unresolvable host fails fast", False, "retried a permanent failure")
    except Exception:
        el2 = time.time() - t0
        check("unresolvable host fails fast", el2 < 2.0, "%.2fs" % el2)

    # (c) Two Turnstile objects in ONE process must not deadlock against each other.
    #     flock is per open file description, so before the fix each object held its own
    #     description and the second call blocked forever on the default timeout=None.
    print("")
    a = Turnstile(host=HOST, port=PORT, key="x")
    b = Turnstile(host=HOST, port=PORT, key="x")
    check("two instances share one lock", a._lock is b._lock)
    a._lock.acquire()
    try:
        check("second instance is reentrant, not deadlocked",
              b._lock.acquire(timeout=2.0) is True, "would hang forever if separate")
        b._lock.release()
    finally:
        a._lock.release()
    got = b.chat("fake/chat-35B", [{"role": "user", "content": "hi"}])
    check("device still usable afterwards", got == "ok")

    # (d) Different devices must not share a lock file. Stripping punctuation out of the
    #     host collided (172.17.7.177 and 17.21.77.177 both became 172177177), which
    #     silently serialised two unrelated Tiinys against each other.
    one = Turnstile(host="172.17.7.177", key="x")
    two = Turnstile(host="17.21.77.177", key="x")
    check("different hosts get different locks", one._lock.path != two._lock.path,
          os.path.basename(one._lock.path))
    check("same host still shares a lock",
          Turnstile(host="172.17.7.177", key="x")._lock is one._lock)
    check("different ports on one device share a lock",
          Turnstile(host="172.17.7.177", port=9098, key="x")._lock is one._lock,
          "one NPU, so they must queue together")

    # (e) Binary responses must survive as bytes, and a declined image must raise
    #     rather than handing the caller a dict where a PNG was promised.
    print("")
    png = t.image("fake/img", "a lighthouse", seed=7)
    check("image returns real bytes", isinstance(png, bytes) and png[:8] == b"\x89PNG\r\n\x1a\n",
          "%d bytes" % len(png))
    DEV.decline_image = True
    try:
        t.image("fake/img", "a lighthouse", seed=7)
        check("declined image raises instead of returning a dict", False, "returned normally")
    except DeviceError as exc:
        check("declined image raises instead of returning a dict", True, str(exc)[:50])
    except Exception as exc:
        check("declined image raises instead of returning a dict", False, type(exc).__name__)
    DEV.decline_image = False

    # (f) A thread releasing a lock it does not hold must be refused BEFORE the file is
    #     unlocked, or the device is handed away while the real owner is still using it.
    print("")
    guard = {"raised": None, "still_held": None}

    def thief():
        try:
            t._lock.release()
            guard["raised"] = False
        except RuntimeError:
            guard["raised"] = True

    t._lock.acquire()
    th2 = threading.Thread(target=thief)
    th2.start(); th2.join()
    # the owner must still hold it: a fresh process must still be locked out
    probe = subprocess.run([sys.executable, "-c",
                            "import fcntl,sys\n"
                            "fh=open(%r,'a+')\n" % t._lock.path +
                            "try:\n"
                            "  fcntl.flock(fh.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB); print('GOT')\n"
                            "except OSError: print('BLOCKED')\n"], capture_output=True, text=True)
    guard["still_held"] = "BLOCKED" in probe.stdout
    t._lock.release()
    check("non-owner release is refused", guard["raised"] is True)
    check("device stayed locked through the attempt", guard["still_held"] is True,
          "would have been stolen mid-inference")

    # (g) The lock file has to be usable by EVERY user sharing the device. A default
    #     umask creates it 0644, so a second user gets PermissionError instead of a lock,
    #     and the two applications never coordinate at all.
    print("")
    import stat as _stat
    lp = t._lock.path
    t._lock.acquire(); t._lock.release()
    mode = _stat.S_IMODE(os.stat(lp).st_mode)
    check("lock file is group/other writable", mode == 0o666, "mode %o" % mode)
    check("lock dir is shared between users", not turnstile_mod.LOCK_DIR.startswith("/var/folders"),
          turnstile_mod.LOCK_DIR)

    srv.shutdown()
    print("\n%s  (%d checks failed)" % ("ALL PASS" if not failures else "FAILURES: " + ", ".join(failures),
                                        len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        _worker()
    else:
        sys.exit(main())
