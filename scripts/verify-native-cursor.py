"""Direct native integration test: no DSH client or model calls, own HWNDs only."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'kernel'))
os.environ['DSH_DESKTOP_INPUT_MODE'] = 'virtual'

import win32api
import win32con as C
import win32gui as W
from dsh_desktop.game.input import InputService
from dsh_desktop.cursor_overlay import get_cursor_overlay


def main():
    events, checks, positions = [], [], []
    state = {'drag_position': None, 'scroll_delta': 0}
    stopped = threading.Event()
    def check(name, passed, detail=None):
        checks.append({'name': name, 'passed': bool(passed), 'detail': detail})
    def wndproc(hwnd, msg, wp, lp):
        if msg in (0x200, 0x201, 0x202, 0x203, 0x204, 0x205, 0x20a):
            events.append((hwnd, msg, wp, lp & 0xffff, (lp >> 16) & 0xffff))
            if msg == 0x200 and wp & 1:
                state['drag_position'] = (lp & 0xffff, (lp >> 16) & 0xffff)
                W.InvalidateRect(hwnd, None, True)
            if msg == 0x20a:
                state['scroll_delta'] += ctypes.c_short((wp >> 16) & 0xffff).value
                W.InvalidateRect(hwnd, None, True)
                return 0
        if msg == C.WM_PAINT:
            dc, paint = W.BeginPaint(hwnd)
            W.FillRect(dc, W.GetClientRect(hwnd), C.COLOR_WINDOW + 1)
            W.DrawText(dc, f"Doraemon test | scroll: {state['scroll_delta']}", -1,
                       W.GetClientRect(hwnd), C.DT_CENTER | C.DT_VCENTER | C.DT_SINGLELINE)
            if state['drag_position']:
                x, y = state['drag_position']
                W.Rectangle(dc, x-8, y-8, x+8, y+8)
            W.EndPaint(hwnd, paint)
            return 0
        return W.DefWindowProc(hwnd, msg, wp, lp)
    wc = W.WNDCLASS()
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = f'DoraemonInputTest{os.getpid()}'
    wc.style = C.CS_DBLCLKS
    wc.lpfnWndProc = wndproc
    W.RegisterClass(wc)
    hwnd = W.CreateWindowEx(C.WS_EX_TOPMOST | C.WS_EX_NOACTIVATE, wc.lpszClassName,
                           'Doraemon direct verification', C.WS_OVERLAPPEDWINDOW,
                           180, 180, 560, 360, 0, 0, wc.hInstance, None)
    canvas = W.CreateWindowEx(0, wc.lpszClassName, '', C.WS_CHILD | C.WS_VISIBLE,
                             10, 65, 510, 220, hwnd, 0, wc.hInstance, None)
    edit = W.CreateWindowEx(0, 'Edit', '', C.WS_CHILD | C.WS_VISIBLE | C.WS_BORDER,
                           10, 10, 510, 35, hwnd, 0, wc.hInstance, None)
    W.ShowWindow(hwnd, C.SW_SHOWNOACTIVATE)
    def pump(seconds=.12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            W.PumpWaitingMessages()
            time.sleep(.005)
    def sample():
        p = wintypes.POINT()
        while not stopped.is_set():
            ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
            positions.append((p.x, p.y))
            time.sleep(.002)
    overlay = None
    previous_foreground = W.GetForegroundWindow()
    try:
        pump()
        service = InputService()
        overlay = get_cursor_overlay()
        overlay.begin_task('direct-native-test')
        sx, sy = W.ClientToScreen(canvas, (100, 80))
        threading.Thread(target=sample, daemon=True).start()
        before = len(events)
        result = service.click_window(hwnd, sx-W.ClientToScreen(hwnd,(0,0))[0],
                                      sy-W.ClientToScreen(hwnd,(0,0))[1], activate=False)
        pump()
        received = [e for e in events[before:] if e[1] == 0x201]
        check('bound click reaches native child at exact coordinates', result.get('success') and
              bool(received) and received[-1][0] == canvas and received[-1][3:] == (100,80)
              and overlay._target == (sx,sy), result)
        before = len(events)
        service._virtual_click(sx, sy, clicks=2, hwnd=hwnd)
        pump()
        check('native double click receives DBLCLK', any(e[1] == 0x203 for e in events[before:]))
        before = len(events)
        service.right_click_screen(sx, sy, hwnd=hwnd)
        pump()
        check('native right click receives down and up',
              {0x204,0x205}.issubset({e[1] for e in events[before:]}))
        before = len(events)
        result = service.scroll(sx, sy, -3, hwnd=hwnd)
        pump()
        check('native scroll receives three wheel events', result.get('success') and
              len([e for e in events[before:] if e[0] == canvas and e[1] == 0x20a]) == 3 and
              state['scroll_delta'] == -360, result)
        before = len(events)
        result = service.swipe_screen((sx,sy),(sx+100,sy+40),hwnd=hwnd)
        pump()
        movement = [e for e in events[before:] if e[1] == 0x200 and e[2] == 1]
        check('native drag receives 20 moves and release at destination', result.get('success') and
              len(movement) == 20 and movement[-1][3:] == (200,120) and
              state['drag_position'] == (200,120) and
              any(e[1] == 0x202 and e[3:] == (200,120) for e in events[before:]), result)
        ex, ey = W.ClientToScreen(edit, (30,15))
        service._virtual_click(ex,ey,hwnd=hwnd)
        pump()
        result = service.type_text('whale-鲸鱼', hwnd=hwnd)
        pump()
        check('virtual Unicode input changes native Edit value', W.GetWindowText(edit) == 'whale-鲸鱼', result)
        result = service.press_key('backspace', hwnd=hwnd)
        pump()
        check('virtual key edits only owned control', W.GetWindowText(edit) == 'whale-鲸', result)
        service.hotkey('ctrl','a',hwnd=hwnd)
        service.type_text('replaced',hwnd=hwnd)
        pump()
        check('virtual select-all replaces only owned Edit text', W.GetWindowText(edit) == 'replaced')
        overlay.action('move', sx, sy, duration=2)
        pump(.3)
        rect1 = W.GetWindowRect(overlay._hwnd)
        overlay.action('move',sx+150,sy+50,duration=2)
        pump(.4)
        rect2 = W.GetWindowRect(overlay._hwnd)
        check('virtual sprite visibly moves between targets', rect1 != rect2 and
              abs(rect2[0]-(sx+150)) <= 2 and abs(rect2[1]-(sy+50)) <= 2,
              {'from':rect1,'to':rect2})
        overlay.action('thinking', duration=.05)
        pump(.1)
        check('thinking resumes while task remains active', overlay._resolved_action(time.monotonic()) == 'thinking')
        check('overlay visible and error-free', W.IsWindowVisible(overlay._hwnd) and overlay.error is None,
              overlay.error)
        overlay.end_task('direct-native-test')
        pump(.1)
        check('finishing task hides sprite', not W.IsWindowVisible(overlay._hwnd))
        if '--bridge' in sys.argv:
            import hashlib
            from concurrent.futures import ThreadPoolExecutor
            import requests
            cfg = json.loads((ROOT / '.runtime/state/kernel-state.json').read_text(encoding='utf-8'))
            client = requests.Session()
            client.headers.update({'Authorization':'Bearer '+cfg['token'],
                                   'X-DSH-Owner':hashlib.sha256(f'native-test-{os.getpid()}'.encode()).hexdigest()})
            base = f"http://127.0.0.1:{cfg['port']}/api/dsh"
            with ThreadPoolExecutor(max_workers=1) as pool:
                def api(path, body=None):
                    future = pool.submit(client.request, 'GET' if body is None else 'POST',
                                         base+path, json=body, timeout=35)
                    while not future.done():
                        pump(.01)
                    response = future.result()
                    response.raise_for_status()
                    return response.json()
                bound = api('/windows/bind', {'hwnd':hwnd,'label':'direct native acceptance'})
                wid = bound['window_id']
                client_rect = W.GetClientRect(hwnd)
                origin = W.ClientToScreen(hwnd,(0,0))
                def norm(x,y):
                    return {'x':round((x-origin[0])*1000/client_rect[2]),
                            'y':round((y-origin[1])*1000/client_rect[3])}
                try:
                    before = len(events)
                    result = api(f'/windows/{wid}/click', {**norm(sx,sy),'window_wait_ms':0})
                    pump()
                    cursor = api('/health')['cursor']
                    check('HTTP click reaches owned native child', result.get('success') and
                          any(e[0]==canvas and e[1]==0x201 for e in events[before:]) and
                          abs(cursor['target'][0]-sx)<=2 and abs(cursor['target'][1]-sy)<=2, result)
                    before = len(events)
                    result = api(f'/windows/{wid}/click', {**norm(sx,sy),'button':'right','window_wait_ms':0})
                    pump()
                    check('HTTP right click reaches owned native child', result.get('success') and
                          any(e[0]==canvas and e[1]==0x204 for e in events[before:]), result)
                    before_scroll = state['scroll_delta']
                    result = api(f'/windows/{wid}/scroll', {**norm(sx,sy),'clicks':-2,'window_wait_ms':0})
                    pump()
                    check('HTTP scroll changes owned window scroll state', result.get('success') and
                          state['scroll_delta'] == before_scroll-240, result)
                    p1,p2 = norm(sx,sy), norm(sx+100,sy+40)
                    before = len(events)
                    result = api(f'/windows/{wid}/swipe', {'x1':p1['x'],'y1':p1['y'],
                                 'x2':p2['x'],'y2':p2['y'],'window_wait_ms':0})
                    pump()
                    cursor = api('/health')['cursor']
                    check('HTTP drag moves the owned object', result.get('success') and
                          any(e[1]==0x200 and e[2]==1 for e in events[before:]) and
                          abs(cursor['target'][0]-(sx+100))<=2 and
                          abs(cursor['target'][1]-(sy+40))<=2, result)
                    api(f'/windows/{wid}/click', {**norm(ex,ey),'window_wait_ms':0})
                    result = api(f'/windows/{wid}/key', {'key':'ctrl+a','window_wait_ms':0})
                    check('HTTP virtual select all accepted', result.get('success'), result)
                    result = api(f'/windows/{wid}/type', {'text':'HTTP-鲸鱼','window_wait_ms':0})
                    pump()
                    check('HTTP Unicode input changes the owned Edit', result.get('success') and
                          W.GetWindowText(edit)=='HTTP-鲸鱼', result)
                    result = api(f'/windows/{wid}/key', {'key':'backspace','window_wait_ms':0})
                    pump()
                    check('HTTP key changes only the owned Edit', result.get('success') and
                          W.GetWindowText(edit)=='HTTP-鲸', result)
                    api('/cursor/state', {'active':True})
                    api('/cursor', {'x':sx,'y':sy,'action':'move'})
                    pump(.25)
                    first = api('/health')['cursor']
                    api('/cursor', {'x':sx+150,'y':sy+50,'action':'move'})
                    pump(.3)
                    second = api('/health')['cursor']
                    check('HTTP virtual cursor target moves without a physical mouse action',
                          first.get('target') != second.get('target') and second.get('visible')
                          and second.get('error') is None, {'from':first,'to':second})
                    health = api('/health')
                    check('HTTP kernel reports virtual mode and loaded revision',
                          health['bridge'].get('input_mode')=='virtual' and
                          health['bridge'].get('input_revision'), health['bridge'])
                finally:
                    api('/cursor/state', {'active':False})
                    api('/cursor', {'visible':False})
                    api(f'/windows/{wid}/unbind', {})
        stopped.set()
        check('real mouse position unchanged for entire test', len(set(positions)) == 1,
              {'samples':len(positions),'positions':list(set(positions))[:5]})
    finally:
        stopped.set()
        if overlay:
            overlay.hide()
        W.DestroyWindow(hwnd)
        W.UnregisterClass(wc.lpszClassName,wc.hInstance)
    report = {'checks':checks,'passed':sum(c['passed'] for c in checks),
              'failed':sum(not c['passed'] for c in checks)}
    output = ROOT / 'docs' / ('cursor-http-results.json' if '--bridge' in sys.argv else 'cursor-native-results.json')
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return int(bool(report['failed']))


if __name__ == '__main__':
    raise SystemExit(main())
