import sys, os, time
from pathlib import Path
os.environ["TVBRIDGE_WIN_GEOMETRY"] = "0,0,1024,728"
sys.path.insert(0, r"C:\tvbridge")
from tvbridge.gui.windriver import WinDriver
from tvbridge.executors.mt5gui import _GuiLock
sym = sys.argv[1] if len(sys.argv) > 1 else "XAGUSD.h"
d = WinDriver()
with _GuiLock(Path(r"C:\tvbridge-home\gui.lock"), 60.0):
    wins = d.list_windows(["terminal64"])
    main = [w for w in wins if "8089020" in w.title][0]
    mw = [i.text for i in d.control_items(main) if ".h" in i.text]
    print("market-watch-ish texts:", mw[:40])
    d.activate(main.pid); time.sleep(0.4)
    d.click(main.x + 500, main.y + 12); time.sleep(0.3)
    before = {w.wid for w in wins}
    d.key("f9")
    dlg = None
    for _ in range(40):
        time.sleep(0.1)
        new = [w for w in d.list_windows(["terminal64"]) if w.wid not in before and "order" in w.title.lower()]
        if new:
            dlg = new[0]; break
    if dlg is None:
        print("no order dialog"); sys.exit(1)
    print("dialog:", dlg.title, dlg.x, dlg.y, dlg.w, dlg.h)
    t0 = time.monotonic()
    d.click(dlg.x + 420, dlg.y + 57); time.sleep(0.15)
    d.key("end"); d.key("home", ("shift",)); d.type_text(sym); d.key("tab")
    t1 = time.monotonic()
    print("typed in %.2fs" % (t1 - t0))
    seen = None
    for i in range(32):
        time.sleep(0.25)
        cur = [w for w in d.list_windows(["terminal64"]) if w.wid == dlg.wid]
        if not cur:
            print("dialog gone"); break
        title = cur[0].title
        if seen != title:
            seen = title
            print("%.2fs title: %s" % (time.monotonic() - t1, title))
            if sym.lower() in title.lower():
                items = d.control_items(cur[0])
                print("   field texts:", [it.text for it in items if sym.split(".")[0] in it.text.upper()][:5])
                print("   quote-ish:", [it.text for it in items if "/" in it.text][:3])
                break
    cur = [w for w in d.list_windows(["terminal64"]) if w.wid == dlg.wid]
    if cur:
        d.capture(cur[0], r"C:\tvbridge-setup\shots\symprobe.png")
        d.activate(main.pid); d.key("escape"); time.sleep(0.8)
        if [w for w in d.list_windows(["terminal64"]) if w.wid == dlg.wid]:
            d.close_window(dlg.wid); time.sleep(0.8)
    print("still open:", [w.title for w in d.list_windows(["terminal64"]) if w.wid not in before])
