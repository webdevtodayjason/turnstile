#!/usr/bin/env bash
# Verify Turnstile against a REAL Tiiny. Run this on the Orange Pi.
#
#   sudo ./verify-on-device.sh
#
# It needs sudo only to read TIINY_HOST/TIINY_KEY out of /etc/warboard.env, which is
# root-only. The key is exported into this script's own environment and never printed.
#
# Safe to run while WARBOARD is live. The calls are deliberately small, and the point of
# the last test is precisely that WARBOARD is competing for the device at the same time:
# that is real contention, which is better evidence than a quiet device would be.
set -uo pipefail
cd "$(dirname "$0")"

ENVFILE=${ENVFILE:-/etc/warboard.env}
if [ -r "$ENVFILE" ]; then
  set -a; . "$ENVFILE"; set +a
else
  echo "cannot read $ENVFILE — run with sudo, or set TIINY_HOST and TIINY_KEY yourself" >&2
  [ -n "${TIINY_KEY:-}" ] || exit 1
fi
export TIINY_HOST TIINY_KEY
[ -n "${TIINY_KEY:-}" ] || { echo "TIINY_KEY is empty after loading $ENVFILE" >&2; exit 1; }

echo "device: ${TIINY_HOST}   (key loaded, not shown)"
echo

echo "== 1. residency =="
python3 turnstile.py || exit 1
echo

echo "== 2. one real inference through the turnstile =="
python3 - <<'PY' || exit 1
import time, turnstile
t = turnstile.Turnstile(on_wait=lambda a, d, why: print("   waited %.1fs (%s)" % (d, why)))
model = next((m for m, u, s in t.budget.resident() if s == "running" and u >= 20), None)
if not model:
    print("   no chat-sized model resident; start one and re-run"); raise SystemExit(1)
t0 = time.time()
out = t.chat(model, [{"role": "user", "content": "Reply with exactly: turnstile ok"}], max_tokens=600)
print("   %s -> %r in %.1fs" % (model, out[:60], time.time() - t0))
PY
echo

echo "== 3. two processes, concurrent, while WARBOARD is also using the device =="
python3 - <<'PY'
import json, os, subprocess, sys, time, turnstile

t = turnstile.Turnstile()
model = next((m for m, u, s in t.budget.resident() if s == "running" and u >= 20), None)
if not model:
    print("   no chat-sized model resident"); raise SystemExit(1)

WORKER = '''
import json, sys, time, turnstile
t = turnstile.Turnstile(tries=12, base_delay=1.0, max_delay=15.0)
ok = refused = 0
for i in range(3):
    try:
        t.chat(sys.argv[1], [{"role": "user", "content": "Say the word blue."}], max_tokens=400)
        ok += 1
    except Exception:
        refused += 1
print(json.dumps({"ok": ok, "refused": refused}))
'''
t0 = time.time()
kids = [subprocess.Popen([sys.executable, "-c", WORKER, model],
                         stdout=subprocess.PIPE, text=True, env=os.environ.copy())
        for _ in range(2)]
res = []
for k in kids:
    out, _ = k.communicate(timeout=600)
    res.append(json.loads(out.strip().splitlines()[-1]))
ok = sum(r["ok"] for r in res)
refused = sum(r["refused"] for r in res)
print("   %d/6 calls completed, %d refused, %.1fs total" % (ok, refused, time.time() - t0))
print("   RESULT:", "PASS — no call was refused under real contention" if refused == 0
      else "FAIL — %d calls were refused" % refused)
raise SystemExit(0 if refused == 0 else 1)
PY
rc=$?
echo
echo "== 4. WARBOARD is unharmed =="
curl -s https://warboard.semfreak.dev/api/stats \
  | python3 -c "import json,sys; c=json.load(sys.stdin)['counts']; print('   items %s | errors_24h %s | pending %s' % (c['items_total'], c['errors_24h'], c['pending']))"
echo "   (errors_24h must still be 0)"
exit $rc
