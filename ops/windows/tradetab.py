import sys, os, time
from pathlib import Path
os.environ["TVBRIDGE_WIN_GEOMETRY"] = "0,0,1024,728"
sys.path.insert(0, r"C:\tvbridge")
from tvbridge.gui.windriver import WinDriver
from tvbridge.executors.mt5gui import _GuiLock
d = WinDriver()
with _GuiLock(Path(r"C:\tvbridge-home\gui.lock"), 90.0):
    main = [w for w in d.list_windows(["terminal64"]) if "8089020" in w.title][0]
    d.activate(main.pid); time.sleep(0.4)
    d.click(main.x + 48, main.y + 690); time.sleep(1.0)      # calibrated Trade tab
    d.capture(main, r"C:\tvbridge-setup\shots\tradetab.png")
    items = d.control_items(main)
    print("toolbox texts:", [i.text for i in items if i.text.strip().lower() in ("trade", "history", "ticket", "change", "balance") or i.text.lower().startswith("balance:")][:10])
