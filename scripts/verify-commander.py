"""Real mouse + native context menu + decorative-layer acceptance, no model."""
import ctypes,json,os,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'kernel'))
import win32api,win32gui as W,win32con as C,win32process
import numpy as np
import mss
import pythoncom
from dsh_desktop.game.input import InputService
from dsh_desktop.cursor_overlay import get_cursor_overlay
from dsh_desktop.decorations import is_decoration_window

def main():
    checks=[]; events=[]; scroll=[0]; drag=[None]
    def check(name,ok,detail=None):checks.append({'name':name,'passed':bool(ok),'detail':detail})
    def proc(hwnd,msg,wp,lp):
        if msg in (0x201,0x202,0x204,0x205,0x200):
            events.append((msg,lp&65535,(lp>>16)&65535))
            if msg==0x200 and wp&1:drag[0]=(lp&65535,(lp>>16)&65535)
        if msg==0x20a:
            scroll[0]+=ctypes.c_short((wp>>16)&65535).value
            return 0
        if msg==C.WM_PAINT:
            dc,paint=W.BeginPaint(hwnd)
            brush=W.CreateSolidBrush(win32api.RGB(30,40,60))
            W.FillRect(dc,W.GetClientRect(hwnd),brush);W.DeleteObject(brush);W.EndPaint(hwnd,paint)
            return 0
        return W.DefWindowProc(hwnd,msg,wp,lp)
    wc=W.WNDCLASS();wc.hInstance=win32api.GetModuleHandle(None)
    wc.lpszClassName=f'CommanderAcceptance{os.getpid()}';wc.lpfnWndProc=proc;W.RegisterClass(wc)
    hwnd=W.CreateWindowEx(C.WS_EX_TOPMOST|C.WS_EX_NOACTIVATE,wc.lpszClassName,'Doraemon physical + mascot test',C.WS_OVERLAPPEDWINDOW,80,120,660,450,0,0,wc.hInstance,None)
    canvas=W.CreateWindowEx(0,wc.lpszClassName,'',C.WS_CHILD|C.WS_VISIBLE,10,60,620,320,hwnd,0,wc.hInstance,None)
    edit=W.CreateWindowEx(0,'Edit','',C.WS_CHILD|C.WS_VISIBLE|C.WS_BORDER,10,10,620,35,hwnd,0,wc.hInstance,None)
    W.ShowWindow(hwnd,C.SW_SHOWNOACTIVATE)
    def pump(seconds=.1):
        end=time.monotonic()+seconds
        while time.monotonic()<end:W.PumpWaitingMessages();time.sleep(.005)
    bridge_mode='--bridge' in sys.argv
    pool=None
    if bridge_mode:
        from concurrent.futures import ThreadPoolExecutor
        import hashlib,requests
        cfg=json.loads((ROOT/'.runtime/state/kernel-state.json').read_text(encoding='utf-8-sig'))
        client=requests.Session();client.headers.update({'Authorization':'Bearer '+cfg['token'],
          'X-DSH-Owner':hashlib.sha256(f'commander-test-{os.getpid()}'.encode()).hexdigest()})
        pool=ThreadPoolExecutor(max_workers=3)
        def api(path,body=None):
            future=pool.submit(client.request,'GET' if body is None else 'POST',
                f"http://127.0.0.1:{cfg['port']}/api/dsh"+path,json=body,timeout=35)
            while not future.done():pump(.01)
            response=future.result();response.raise_for_status();return response.json()
        pump()
        binding=api('/windows/bind',{'hwnd':hwnd,'label':'commander acceptance owned window'})
        wid=binding['window_id']
        def norm(x,y):
            ox,oy=W.ClientToScreen(hwnd,(0,0));r=W.GetClientRect(hwnd)
            return {'x':round((x-ox)*1000/r[2]),'y':round((y-oy)*1000/r[3])}
        class RemoteInput:
            virtual=False
            def click_screen(self,x,y):return api(f'/windows/{wid}/click',{**norm(x,y),'window_wait_ms':0})
            def right_click_screen(self,x,y):return api(f'/windows/{wid}/click',{**norm(x,y),'button':'right','window_wait_ms':0})
            def scroll(self,x,y,clicks):return api(f'/windows/{wid}/scroll',{**norm(x,y),'clicks':clicks,'window_wait_ms':0})
            def swipe_screen(self,p1,p2,speed=1):
                a,b=norm(*p1),norm(*p2)
                return api(f'/windows/{wid}/swipe',{'x1':a['x'],'y1':a['y'],'x2':b['x'],'y2':b['y'],'speed':speed,'window_wait_ms':0})
            def press_key(self,key,*_):return api(f'/windows/{wid}/key',{'key':key,'window_wait_ms':0})
            def type_text(self,text):return api(f'/windows/{wid}/type',{'text':text,'window_wait_ms':0})
        service=RemoteInput()
    else:service=InputService()
    original=W.GetCursorPos();overlay=None;clipboard=None
    try:
        pump()
        ex,ey=W.ClientToScreen(edit,(80,15))
        dock=(ex-80+40,ey-15-10)
        overlay=get_cursor_overlay();overlay._layout=lambda:(dock,dock)
        before=W.GetForegroundWindow();overlay.begin_task('native-acceptance');pump(.3)
        check('summon does not change foreground',before==W.GetForegroundWindow())
        overlay._born-=3;pump(.15)
        fixed=W.GetWindowRect(overlay._hwnd)
        check('decoration is visible and error-free',W.IsWindowVisible(overlay._hwnd) and not overlay.error,overlay.error)
        styles=W.GetWindowLong(overlay._hwnd,C.GWL_EXSTYLE)
        check('transparent layered no-activate tool window',all(styles&flag for flag in (0x20,0x80000,0x08000000,0x80,0x8)))
        check('bubble passes native hit testing to Edit',W.WindowFromPoint((ex,ey))==edit)
        sx,sy=W.ClientToScreen(canvas,(100,100))
        check('commander is in decoration allowlist only',is_decoration_window(overlay._hwnd) and not is_decoration_window(hwnd))
        box={'left':dock[0]+178,'top':dock[1]+85,'width':100,'height':105}
        with mss.mss() as capture:
            shown=np.array(capture.grab(box))
            with overlay.capture_guard(): hidden=np.array(capture.grab(box))
        check('decoration excluded from screen capture',overlay._capture_excluded and np.array_equal(shown,hidden),{'display_affinity':overlay._capture_excluded,'different_pixels':int(np.count_nonzero(shown!=hidden))})
        overlay.action('click');service.click_screen(ex,ey);pump()
        actual=W.GetCursorPos()
        check('real mouse moves to click target',abs(actual[0]-ex)<=2 and abs(actual[1]-ey)<=2,{'actual':actual,'expected':(ex,ey)})
        check('click through bubble focuses native Edit',W.GetForegroundWindow()==hwnd)
        menu_seen=[]
        def dismiss_own_menu():
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                menu=W.FindWindow('#32768',None)
                if menu and win32process.GetWindowThreadProcessId(menu)[1]==os.getpid():
                    menu_seen.append(menu)
                    if W.GetForegroundWindow()==hwnd:service.press_key('escape',.03)
                    return
                time.sleep(.02)
        watcher=threading.Thread(target=dismiss_own_menu,daemon=True);watcher.start()
        overlay.action('right_click');service.right_click_screen(ex,ey);pump(.4);watcher.join(timeout=4.5)
        check('physical right click opens native Edit context menu',bool(menu_seen))
        check('commander stays docked during physical right click',W.GetWindowRect(overlay._hwnd)==fixed)
        overlay.action('scroll');service.scroll(sx,sy,-3);pump()
        check('physical wheel delivers three detents',scroll[0]==-360,scroll[0])
        overlay.action('drag');service.swipe_screen((sx,sy),(sx+100,sy+40),speed=3);pump()
        actual=W.GetCursorPos()
        check('physical drag reaches destination',abs(actual[0]-(sx+100))<=2 and abs(actual[1]-(sy+40))<=2 and drag[0] is not None and abs(drag[0][0]-200)<=2 and abs(drag[0][1]-140)<=2,drag[0])
        check('character never follows real pointer',W.GetWindowRect(overlay._hwnd)==fixed)
        service.click_screen(ex,ey);pump()
        pythoncom.OleInitialize()
        try:clipboard=pythoncom.OleGetClipboard()
        except pythoncom.com_error:clipboard=None
        if clipboard is not None:
            overlay.action('type');result=service.type_text('鲸鱼魔法');pump(.2)
            check('physical clipboard typing reaches Edit',result.get('success') and W.GetWindowText(edit)=='鲸鱼魔法')
        overlay.end_task('native-acceptance');pump(.1)
        check('task completion leaves no visible character',not W.IsWindowVisible(overlay._hwnd))
        overlay.action('click');pump(.05)
        check('late tool feedback cannot resurrect character',not W.IsWindowVisible(overlay._hwnd))
        overlay.begin_task('lease',lease_seconds=.1);pump(.5)
        check('abandoned lease self-cleans',not W.IsWindowVisible(overlay._hwnd))
        import subprocess
        child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(.3)'])
        overlay.begin_task('process',client_pid=child.pid)
        child.wait(timeout=5);pump(.5)
        check('owner process exit removes character',not W.IsWindowVisible(overlay._hwnd))
        check('input is physical',not service.virtual)
    finally:
        if overlay:overlay.end_task('native-acceptance');overlay.end_task('lease');overlay.end_task('process');overlay.hide()
        if clipboard is not None:
            try:pythoncom.OleSetClipboard(clipboard);pythoncom.OleFlushClipboard()
            except pythoncom.com_error:pass
        W.DestroyWindow(hwnd);W.UnregisterClass(wc.lpszClassName,wc.hInstance)
        if bridge_mode:
            api(f'/windows/{wid}/unbind',{})
            pool.shutdown(wait=True)
        else:service.mouse.position=original # test cleanup only; normal actions never restore
    report={'checks':checks,'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks)}
    (ROOT/'docs/commander-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return int(bool(report['failed']))
if __name__=='__main__':raise SystemExit(main())
