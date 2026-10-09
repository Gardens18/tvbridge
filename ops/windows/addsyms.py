import sys, os, time
from pathlib import Path
os.environ["TVBRIDGE_WIN_GEOMETRY"] = "0,0,1024,728"
sys.path.insert(0, r"C:\tvbridge")
from tvbridge.gui.windriver import WinDriver
from tvbridge.executors.mt5gui import _GuiLock
syms = sys.argv[1:] or ["XAGUSD.h"]
d = WinDriver()
with _GuiLock(Path(r"C:\tvbridge-home\gui.lock"), 60.0):
    for sym in syms:
        main = [w for w in d.list_windows(["terminal64"]) if "8089020" in w.title][0]
        d.activate(main.pid); time.sleep(0.5)
        before = {w.wid for w in d.list_windows(["terminal64"])}
        d.click(main.x + 500, main.y + 12); time.sleep(0.3)
        d.key("u", ("ctrl",)); time.sleep(2.5)
        new = [w for w in d.list_windows(["terminal64"]) if w.wid not in before]
        if not new:
            print(sym, "symbols window did not open"); continue
        w = new[0]; print(sym, "dialog", w.title, w.x, w.y, w.w, w.h)
        d.click(w.x + 350, w.y + 78); time.sleep(0.4); d.type_text(sym); time.sleep(3.0)
        png = r"C:\tvbridge-setup\shots\sym_%s_a.png" % sym.split(".")[0]
        sc = d.capture(w, png); items = d.ocr(png, w, sc)
        rows = [i for i in items if sym.upper().replace(" ", "") in i.text.upper().replace(" ", "") and i.conf < 1.0]
        print("  rows", [(i.text, round(i.x), round(i.y), round(i.conf, 2)) for i in rows][:5])
        btn = [i for i in items if i.text.strip() == "Show Symbol"]
        print("  button", [(round(i.cx), round(i.cy)) for i in btn])
        if rows and btn:
            r = sorted(rows, key=lambda i: i.y)[0]
            d.click(r.cx, r.cy); time.sleep(0.8)
            d.click(btn[0].cx, btn[0].cy); time.sleep(1.0)
        d.key("escape"); time.sleep(1.0)
        left = [x.title for x in d.list_windows(["terminal64"]) if x.wid not in before]
        if left:
            d.close_window(new[0].wid); time.sleep(1.0)
            left = [x.title for x in d.list_windows(["terminal64"]) if x.wid not in before]
        print("  still open:", left)
