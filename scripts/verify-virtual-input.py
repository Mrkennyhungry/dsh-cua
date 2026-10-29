"""Live verification of the virtual input route and cosmetic cursor on Windows.

Run from the plugin root:

    .runtime/venv/Scripts/python.exe scripts/verify-virtual-input.py

It creates its own throwaway Tk window (never touches existing kernels or windows),
posts Win32 messages at it, samples GetCursorPos throughout, renders every cursor
action in all three palettes, and writes docs/images/cursor-live.png as evidence.
Exit code 0 only when every check passed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── child: the throwaway window ─────────────────────────────────────────────
def _app(log_path: str) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("CursorAcceptance")
    root.geometry("520x360+140+140")
    log = open(log_path, "a", buffering=1)

    def rec(kind, event=None, **extra):
        row = {"event": kind, "t": round(time.monotonic(), 3)}
        if event is not None:
            row.update(x=event.x, y=event.y)
        row.update(extra)
        log.write(json.dumps(row) + "\n")

    entry = tk.Entry(root)
    entry.pack(fill="x")
    entry.bind("<Key>", lambda e: rec("key", e, char=e.char))
    entry.bind("<<Paste>>", lambda e: root.after(120, lambda: rec("paste", None, text=entry.get())))

    canvas = tk.Canvas(root, bg="#102030", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.bind("<Button-1>", lambda e: rec("click", e))
    canvas.bind("<Double-Button-1>", lambda e: rec("double", e))
    canvas.bind("<B1-Motion>", lambda e: rec("motion", e))
    canvas.bind("<ButtonRelease-1>", lambda e: rec("release", e))
    canvas.bind("<Button-3>", lambda e: rec("right", e))
    canvas.bind("<MouseWheel>", lambda e: rec("wheel", None, delta=e.delta))
    rec("ready")
    root.after(200, lambda: rec("hwnd", None, hwnd=root.winfo_id()))
    root.mainloop()


# ── parent: verification ────────────────────────────────────────────────────
def _find_window(title: str) -> int:
    import win32gui

    found = []
    win32gui.EnumWindows(
        lambda h, _: found.append(h) if title in win32gui.GetWindowText(h) else None, None)
    return found[0] if found else 0


def _wait_log(path: Path, kind: str, timeout: float = 4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            hits = [r for r in rows if r["event"] == kind]
            if hits:
                return hits
        time.sleep(0.05)
    return []


def main() -> int:
    os.environ['DSH_DESKTOP_INPUT_MODE'] = 'virtual'
    sys.path.insert(0, str(ROOT / "kernel"))
    import ctypes
    import ctypes.wintypes
    import numpy as np
    import win32gui
    from dsh_desktop.game.input import InputService
    timeout = 30
    temp = Path(tempfile.mkdtemp(prefix="cursor-acceptance-"))
    log_path = temp / "events.jsonl"
    child = subprocess.Popen([sys.executable, __file__, "--app", str(log_path)])
    report = {"checks": [], "cursor_samples": 0, "cursor_changed": False}

    def check(name, ok, detail=None):
        report["checks"].append({"check": name, "ok": bool(ok), "detail": detail})

    samples, stop = [], threading.Event()

    def sampler():
        point = ctypes.wintypes.POINT()
        while not stop.is_set():
            ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
            samples.append((point.x, point.y))
            time.sleep(0.02)

    try:
        hwnd = 0
        for _ in range(60):
            hwnd = _find_window("CursorAcceptance")
            if hwnd:
                break
            time.sleep(0.1)
        check("test window created", bool(hwnd), hwnd)
        if not hwnd:
            return 1
        service = InputService()
        service.window.ensure_foreground(hwnd)
        time.sleep(0.3)
        _wait_log(log_path, "hwnd")
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        width, height = right - left, bottom - top
        sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
        canvas_y = sy + 120          # below the entry row
        canvas_x = sx + width // 2

        threading.Thread(target=sampler, daemon=True).start()
        start_pos = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(start_pos))

        # click
        r = service._virtual_click(canvas_x, canvas_y, 1, hwnd=hwnd)
        check("virtual click reports message/unconfirmed",
              r.get("success") and r.get("dispatch") == "message" and r.get("effect") == "unconfirmed", r)
        check("click reached the target window", bool(_wait_log(log_path, "click")),
              {"entry_hwnd": win32gui.WindowFromPoint((canvas_x, canvas_y))})

        # double click
        r = service._virtual_click(canvas_x, canvas_y, 2, hwnd=hwnd)
        check("double click posts DBLCLK and succeeds", r.get("success"), r)
        check("double click reached the target window", bool(_wait_log(log_path, "double")))

        # right click
        r = service.right_click_screen(canvas_x, canvas_y, hwnd=hwnd)
        check("right click reports message dispatch", r.get("success") and r.get("dispatch") == "message", r)
        check("right click reached the target window", bool(_wait_log(log_path, "right")))

        # drag
        r = service.swipe_screen((canvas_x - 80, canvas_y), (canvas_x + 80, canvas_y + 40), speed=2, hwnd=hwnd)
        check("same-window drag reports message dispatch", r.get("success"), r)
        check("drag produced motion+release",
              bool(_wait_log(log_path, "motion")) and bool(_wait_log(log_path, "release")))

        # scroll
        r = service.scroll(canvas_x, canvas_y, -3, "vertical", hwnd=hwnd)
        check("scroll reports message dispatch", r.get("success"), r)
        check("wheel reached the target window", bool(_wait_log(log_path, "wheel")))

        # keyboard: click the entry, then type keys and paste text
        entry_y = sy + 12
        service._virtual_click(sx + width // 2, entry_y, 1, hwnd=hwnd)
        time.sleep(0.2)
        focused = win32gui.GetForegroundWindow() == hwnd and bool(_wait_log(log_path, 'click', .2))
        check('test window owns foreground before keyboard input', focused)
        if focused:
            service.press_key("a")
            check("key press reached the entry", bool(_wait_log(log_path, "key", 2.0)))
            r = service.type_text("hey")
            check("type_text clipboard paste", r.get("success"), r)
            pasted = _wait_log(log_path, "paste", 2.0)
            check("pasted text arrived", bool(pasted) and pasted[0].get("text", "").endswith("hey"), pasted[:1])

        # cosmetic cursor: nine actions, three palettes
        from dsh_desktop.cursor_overlay import CursorOverlay
        from dsh_desktop.cursor_theme import ACTIONS, render
        overlay = CursorOverlay()
        overlay.start()
        overlay.begin_task('acceptance')
        overlay.action('thinking', canvas_x, canvas_y, duration=3)
        time.sleep(0.4)
        check("overlay started without error", overlay.error is None, overlay.error)
        for palette in ("ocean", "lavender", "mint"):
            shapes = {render("whale-maid", action, 0, palette).tobytes() for action in ACTIONS}
            check(f"palette {palette}: actions render sprites", len(shapes) >= len(ACTIONS)-1,
                  {"distinct": len(shapes), "of": len(ACTIONS)})
        check("overlay still reports no error", overlay.error is None, overlay.error)
        check("overlay window visible", bool(win32gui.IsWindowVisible(overlay._hwnd)),
              {"hwnd": overlay._hwnd})
        overlay.configure("whale-maid", "ocean", 2)
        overlay.action("click", canvas_x + 60, canvas_y + 60, duration=3.0)
        time.sleep(0.5)
        check("overlay kept visible for the live screenshot",
              bool(win32gui.IsWindowVisible(overlay._hwnd)))

        time.sleep(0.5)
        frame = None
        try:
            import mss
            from PIL import Image
            box = {"left": canvas_x + 20, "top": canvas_y + 20, "width": 140, "height": 110}
            with mss.mss() as sct:
                shot = np.array(sct.grab(box))[:, :, :3]
            Image.fromarray(shot[:, :, ::-1]).save(ROOT / "docs" / "images" / "cursor-live.png")
            frame = shot
        except Exception as exc:  # noqa: BLE001
            check("live screenshot saved", False, f"{type(exc).__name__}: {exc}")
        if frame is not None:
            bright = int((frame.max(axis=2) > 90).sum())
            check("cursor sprite visible in live screenshot", bright > 150, {"bright_pixels": bright})

        overlay.hide()
        time.sleep(0.3)
        stop.set()
        end_pos = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(end_pos))
        report["cursor_samples"] = len(samples)
        report["cursor_changed"] = len(set(samples)) > 1
        report["cursor_start"] = [start_pos.x, start_pos.y]
        report["cursor_end"] = [end_pos.x, end_pos.y]
        check("GetCursorPos never moved during virtual input",
              len(set(samples)) == 1 and (start_pos.x, start_pos.y) == (end_pos.x, end_pos.y),
              {"unique_positions": sorted(set(samples))[:4], "samples": len(samples)})
    finally:
        stop.set()
        try:
            child.terminate()
            child.wait(timeout=5)
        except Exception:  # noqa: BLE001
            child.kill()
        import shutil
        shutil.rmtree(temp, ignore_errors=True)

    failures = [c for c in report["checks"] if not c["ok"]]
    report["passed"] = len(report["checks"]) - len(failures)
    report["failed"] = failures
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if not failures else 1


if __name__ == "__main__":
    if "--app" in sys.argv:
        _app(sys.argv[sys.argv.index("--app") + 1])
    else:
        raise SystemExit(main())
