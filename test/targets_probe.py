"""Owned live P2 fixtures. Uses the runtime entry points and an isolated profile."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.request import urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor

from dsh_desktop.dpi import set_per_monitor_v2

set_per_monitor_v2()

import win32con
import win32gui
from dsh_desktop.runtime import DesktopRuntime, RuntimeConfig
from dsh_desktop.targets import RuntimeFault


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        pages = {
            '/': '<button id="same" onclick="this.textContent=\'main clicked\'">same</button>'
                 '<iframe name="outer" src="/outer"></iframe><div style="height:2400px"></div>',
            '/outer': '<button id="same" onclick="this.textContent=\'outer clicked\'">same</button>'
                      '<iframe name="inner" src="/inner"></iframe>',
            '/inner': '<button id="same" onclick="this.textContent=\'inner clicked\'">same</button>',
        }
        data = ('<!doctype html><html><body>' + pages.get(self.path, '<p>next document</p>') + '</body></html>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def fault(code, function):
    try:
        function()
    except RuntimeFault as error:
        assert error.code == code, (error.code, code)
        return
    raise AssertionError('Expected ' + code)


home = Path(sys.argv[1]).resolve()
home.mkdir(parents=True, exist_ok=True)
os.environ['DSH_DESKTOP_HOME'] = str(home)
os.environ['DSH_DESKTOP_STATE_DIR'] = str(home / 'state')
with socket.socket() as reservation:
    reservation.bind(('127.0.0.1', 0))
    os.environ['DSH_DESKTOP_CDP_PORT'] = str(reservation.getsockname()[1])
runtime = DesktopRuntime(RuntimeConfig.from_home(home), home=home)
runtime.setup()
server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
title = uuid.uuid4().hex
child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('test_cleanup_dialog_live.py')), '--child', title],
                         creationflags=subprocess.CREATE_NO_WINDOW)
start = time.monotonic()
report = {}
external = None
try:
    hwnd = 0
    for _ in range(100):
        hwnd = win32gui.FindWindow(None, 'DSH temporary unsaved editor ' + title)
        if hwnd:
            break
        time.sleep(0.1)
    assert hwnd
    native = runtime.open_target('a', hwnd=hwnd)
    native_b = runtime.open_target('b', hwnd=hwnd)
    image, frame = runtime.capture_target('a', native.target_id)
    assert frame.geometry.source_rect == native.metadata['window_rect']
    report['native_capture'] = {'width': image.shape[1], 'height': image.shape[0], **frame.geometry.metadata()}
    revision = native.revision
    win32gui.MoveWindow(hwnd, 200, 160, 600, 240, True)
    fault('FRAME_STALE', lambda: runtime.map_point('a', native.target_id, frame.frame_id, 10, 10))
    assert native.revision > revision
    image, frame = runtime.capture_target('a', native.target_id, crop=(20, 30, 200, 100), max_dim=100)
    assert runtime.map_point('a', native.target_id, frame.frame_id, 50, 25) == (320, 240)
    report['native_move_resize_crop'] = {'revision': native.revision, 'mapped': [320, 240]}
    desktop = runtime.open_target('a')
    image, desktop_frame = runtime.capture_target('a', desktop.target_id)
    report['desktop_capture'] = {'image_size': [image.shape[1], image.shape[0]],
                                 'source_rect': desktop_frame.geometry.source_rect}

    url = f'http://127.0.0.1:{server.server_port}/'
    first = runtime.open_target('a', url=url, call_id='first')
    assert runtime.browser.thread_id != threading.get_ident()
    lock_probe = subprocess.run([sys.executable, '-c', '''
import json, sys
from pathlib import Path
from dsh_desktop.runtime import DesktopRuntime, RuntimeConfig
from dsh_desktop.targets import RuntimeFault
home = Path(sys.argv[1])
runtime = DesktopRuntime(RuntimeConfig.from_home(home), home=home)
try:
    runtime.open_target('lock', url='about:blank', call_id='lock')
    raise AssertionError('Occupied profile unexpectedly opened')
except RuntimeFault as error:
    assert error.code == 'BROWSER_PROFILE_UNAVAILABLE', error.code
    print(json.dumps({'profile_lock_diagnosed': 1}))
finally:
    runtime.shutdown()
''', str(home)], capture_output=True, text=True, timeout=30)
    assert lock_probe.returncode == 0, lock_probe.stderr
    report['profile_lock_diagnosed'] = json.loads(lock_probe.stdout.splitlines()[-1])['profile_lock_diagnosed']
    fault('BACKEND_UNAVAILABLE', lambda: runtime.open_target('a', connect='http://127.0.0.1:1'))
    report['failed_explicit_connection_diagnosed'] = 1
    second = runtime.open_target('a', url=url, call_id='second')
    other = runtime.open_target('b', url=url, call_id='other')
    assert first.metadata['tab_id'] != second.metadata['tab_id']
    assert runtime.open_target('a', url=url, call_id='first') is first
    fault('REQUEST_CONFLICT', lambda: runtime.open_target('a', url=url + 'next', call_id='first'))
    frames = runtime.browser.frames('a', first.target_id)
    assert len(frames) == 3, frames
    inner = next(row for row in frames if row['frame_name'] == 'inner')

    def click_inner():
        target = runtime.browser.resolve('a', inner['target_id'])
        target.handle[1].locator('#same').click()
        assert target.handle[1].locator('#same').inner_text() == 'inner clicked'
        assert runtime.browser.resolve('a', second.target_id).handle[1].locator('#same').inner_text() == 'same'
        assert first.handle[1].locator('#same').inner_text() == 'same'

    runtime.browser.call(click_inner)
    image, browser_frame = runtime.capture_target('a', inner['target_id'])
    assert browser_frame.geometry.coordinate_space == 'browser_css'
    report['browser_identity'] = {'same_url_tabs': 3, 'nested_frames': len(frames), 'wrong_instance_clicks': 0,
                                  'image_size': [image.shape[1], image.shape[0]]}
    _, previous = runtime.capture_target('a', first.target_id)
    runtime.browser.call(lambda: first.handle[1].evaluate('scrollTo(0, 500)'))
    fault('FRAME_STALE', lambda: runtime.map_point('a', first.target_id, previous.frame_id, 10, 10))
    document = first.metadata['document_id']
    runtime.open_target('a', target_id=first.target_id, url=url + 'next', call_id='navigate')
    assert first.metadata['document_id'] != document
    report['browser_scroll_navigation_invalidations'] = 2

    def set_state():
        context = second.handle[0].context
        context.add_cookies([{'name': 'fixture_session', 'value': title, 'url': url, 'expires': time.time() + 3600}])
        second.handle[1].evaluate("localStorage.setItem('fixture_setting', 'retained')")
    runtime.browser.call(set_state)

    # Connect explicitly to this owned test browser. Both handles resolve to the
    # same real tab, even across CDP connection wrappers.
    connected = runtime.open_target('b', connect='http://127.0.0.1:' + os.environ['DSH_DESKTOP_CDP_PORT'])
    assert any(row['tab_id'] == second.metadata['tab_id'] for row in connected)
    shared = next(row for row in connected if row['tab_id'] == second.metadata['tab_id'])
    started = threading.Event()
    def ongoing_read():
        def read():
            started.set()
            return other.handle[1].evaluate('() => new Promise(resolve => setTimeout(() => resolve(42), 300))')
        return runtime.browser.call(read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(ongoing_read)
        assert started.wait(2)
        runtime.release_owner('a')
        assert pending.result(timeout=5) == 42
    report['other_owner_in_progress_read_survived'] = 1
    assert runtime.resolve_target('b', other.target_id)
    assert runtime.resolve_target('b', shared['target_id'])
    assert runtime.resolve_target('b', native_b.target_id)
    report['owner_cleanup'] = {'released_targets': len(runtime.targets.directory('a')), 'surviving_targets': len(runtime.targets.directory('b'))}
    profile = str(runtime.browser.profile)
    runtime.browser.shutdown()
    fault('TARGET_CLOSED', lambda: runtime.resolve_target('b', other.target_id))
    reopened = runtime.open_target('b', url=url, call_id='after-restart')
    assert str(runtime.browser.profile) == profile

    def check_state():
        cookies = reopened.handle[0].context.cookies(url)
        assert any(cookie['name'] == 'fixture_session' and cookie['value'] == title for cookie in cookies)
        assert reopened.handle[1].evaluate("localStorage.getItem('fixture_setting')") == 'retained'
    runtime.browser.call(check_state)
    report['persistent_profile'] = {'cookie_restored': 1, 'site_setting_restored': 1, 'restart_cycles': 1,
                                     'profile_under_state_dir': Path(profile).is_relative_to(runtime.state_dir)}
    runtime.browser.close_target('b', reopened.target_id)
    fault('TARGET_CLOSED', lambda: runtime.resolve_target('b', reopened.target_id))
    win32gui.PostMessage(hwnd, win32con.WM_APP + 17, 0, 0)
    child.wait(timeout=5)
    fault('TARGET_CLOSED', lambda: runtime.resolve_target('b', native_b.target_id))
    report['closed_targets_rejected'] = 3
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        external_port = reservation.getsockname()[1]
    external = subprocess.Popen([sys.executable, '-c', '''
import sys
from playwright.sync_api import sync_playwright
from dsh_desktop.web_tools import _edge_executable
with sync_playwright() as pw:
    context = pw.chromium.launch_persistent_context(sys.argv[1], executable_path=_edge_executable(),
        headless=True, args=['--remote-debugging-port=' + sys.argv[2]])
    context.pages[0].goto('data:text/html,<title>External owned browser</title>')
    print('ready', flush=True)
    sys.stdin.read()
    context.close()
''', str(home / 'external-profile'), str(external_port)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    assert external.stdout.readline().strip() == 'ready'
    attached = runtime.open_target('external-owner', connect=f'http://127.0.0.1:{external_port}')
    assert len(attached) == 1
    runtime.release_owner('external-owner')
    runtime.shutdown()
    with urlopen(f'http://127.0.0.1:{external_port}/json/list', timeout=5) as response:
        remaining = json.load(response)
    assert external.poll() is None and any(row.get('title') == 'External owned browser' for row in remaining)
    report['external_browser_survived_release_and_unload'] = 1
    report['parser_initializations'] = runtime.parser_initializations
    report['elapsed_ms'] = round((time.monotonic() - start) * 1000, 3)
finally:
    runtime.shutdown()
    if external is not None:
        external.communicate('', timeout=10)
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    if child.poll() is None:
        child.terminate()
        child.wait(timeout=5)
print(json.dumps(report))
