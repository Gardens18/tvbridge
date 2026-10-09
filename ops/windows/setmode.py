import json, sys, os, time
mode = sys.argv[1]; assert mode in ("live", "rehearsal")
p = r"C:\tvbridge-home\config.json"; c = json.load(open(p))
old = c["executor"]["mode"]; c["executor"]["mode"] = mode
json.dump(c, open(p + ".tmp", "w"), indent=2); os.replace(p + ".tmp", p)
hb = r"C:\tvbridge-home\heartbeat.json"
try: pid = json.load(open(hb))["pid"]
except Exception: pid = None
if pid: os.system("taskkill /PID %d /F >NUL 2>&1" % pid)
print("config mode: %s -> %s; engine restarting..." % (old, mode))
for _ in range(60):
    time.sleep(2)
    try:
        h = json.load(open(hb))
        if h.get("pid") != pid and h.get("mode") == mode:
            print("engine is running in %s mode (pid %s); last account error: %r" % (h["mode"], h["pid"], h.get("last_account_error", "")))
            break
    except Exception: pass
else: print("engine did not come back in %s mode within 2 minutes: check C:\\tvbridge-home\\engine.out" % mode)
