import os, sys, logging
os.environ["TVBRIDGE_HOME"] = r"C:\tvbridge-home"
sys.path.insert(0, r"C:\tvbridge")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
from tvbridge.config import load_config
from tvbridge.notify import Notifier
n = Notifier(load_config().notify)
n.send("Hantec bridge: test", "Telegram check from the server after the certificate fix (2026-10-08). Nothing traded.", "warn")
n.flush()
print("sent (no 'failed' line above = delivered)")
