"""Keep XAUUSD.h / XAUEUR.h / XAGUSD.h in MT5's Market Watch for good: give each an open
chart (MT5 only auto-hides symbols that no chart or position uses). Graceful MT5 exit so the
profile is saved, chart files added, MT5 restarted, symbols verified in the order window."""
import sys, os, time, subprocess, shutil, re
from pathlib import Path
os.environ["TVBRIDGE_WIN_GEOMETRY"] = "0,0,1024,728"
sys.path.insert(0, r"C:\tvbridge")
from tvbridge.gui.windriver import WinDriver
from tvbridge.executors.mt5gui import _GuiLock
PROFILE = Path(r"C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Profiles\Charts\Default")
WANT = [("XAUUSD.h", "Gold vs US Dollar", 2), ("XAUEUR.h", "Gold vs EURO", 2), ("XAGUSD.h", "Silver vs US Dollar", 3)]
EXE = r"C:\Program Files\MetaTrader 5\terminal64.exe"
d = WinDriver()

def mt5_windows():
    return d.list_windows(["terminal64"])

def main_win():
    m = [w for w in mt5_windows() if "8089020" in w.title]
    return m[0] if m else None

def running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq terminal64.exe"], capture_output=True, text=True).stdout
    return "terminal64.exe" in out

with _GuiLock(Path(r"C:\tvbridge-home\gui.lock"), 120.0):
    m = main_win()
    if m is None:
        print("MT5 main window not found; abort"); sys.exit(1)
    print("closing MT5 gracefully")
    d.close_window(m.wid)
    for i in range(60):
        time.sleep(1.0)
        if not running():
            break
    if running():
        print("MT5 did not exit within 60 s; NOT killing it; abort"); sys.exit(1)
    print("MT5 exited after %d s" % (i + 1))
    time.sleep(2.0)
    existing = sorted(PROFILE.glob("chart*.chr"))
    have = {}
    for p in existing:
        mm = re.search(r"^symbol=(.*)$", p.read_text(encoding="utf-8", errors="replace"), re.M)
        if mm:
            have[mm.group(1).strip()] = p.name
    print("charts before:", have)
    template = (PROFILE / "chart01.chr").read_text(encoding="utf-8", errors="replace")
    n = len(existing)
    base_id = 128968168864101562
    for k, (sym, desc, digits) in enumerate(WANT):
        if sym in have:
            continue
        n += 1
        txt = template
        txt = re.sub(r"^id=.*$", "id=%d" % (base_id + 7000 + k), txt, flags=re.M)
        txt = re.sub(r"^symbol=.*$", "symbol=%s" % sym, txt, flags=re.M)
        txt = re.sub(r"^description=.*$", "description=%s" % desc, txt, flags=re.M)
        txt = re.sub(r"^digits=.*$", "digits=%d" % digits, txt, flags=re.M)
        txt = re.sub(r"^scale_fixed_min=.*$", "scale_fixed_min=0.000000", txt, flags=re.M)
        txt = re.sub(r"^scale_fixed_max=.*$", "scale_fixed_max=0.000000", txt, flags=re.M)
        name = "chart%02d.chr" % n
        (PROFILE / name).write_text(txt, encoding="utf-8")
        print("wrote", name, sym)
    print("starting MT5")
    subprocess.Popen([EXE], cwd=os.path.dirname(EXE))
    for i in range(60):
        time.sleep(1.0)
        m = main_win()
        if m is not None and m.w > 500:
            break
    time.sleep(12.0)
    m = main_win()
    print("main:", m.title if m else None, m.x if m else "", m.y if m else "", m.w if m else "", m.h if m else "")
    d.capture(m, r"C:\tvbridge-setup\shots\persist_main.png")
    # verify each symbol in the order window
    for sym, _, _ in WANT:
        d.activate(m.pid); time.sleep(0.4)
        d.click(m.x + 500, m.y + 12); time.sleep(0.3)
        before = {w.wid for w in mt5_windows()}
        d.key("f9")
        dlg = None
        for _ in range(40):
            time.sleep(0.1)
            new = [w for w in mt5_windows() if w.wid not in before and "order" in w.title.lower()]
            if new:
                dlg = new[0]; break
        if dlg is None:
            print(sym, "no order dialog"); continue
        d.click(dlg.x + 420, dlg.y + 57); time.sleep(0.15)
        d.key("end"); d.key("home", ("shift",)); d.type_text(sym); d.key("tab")
        title = ""
        for _ in range(12):
            time.sleep(0.25)
            cur = [w for w in mt5_windows() if w.wid == dlg.wid]
            if not cur: break
            title = cur[0].title
            if sym.lower() in title.lower(): break
        print(sym, "->", title)
        d.activate(m.pid); d.key("escape"); time.sleep(0.6)
        if [w for w in mt5_windows() if w.wid == dlg.wid]:
            d.close_window(dlg.wid); time.sleep(0.6)
    print("open extra windows:", [w.title for w in mt5_windows() if w.wid != m.wid and w.w > 20])
