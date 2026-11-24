"""Owned live P2 fixtures. Uses the runtime entry points and an isolated profile."""
import json
import os
from pathlib import Path
import socket
import shutil
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
config = RuntimeConfig.from_home(home)
if '--vision' in sys.argv:
    config.workspace.weights_dir = str(Path(os.environ['LOCALAPPDATA']) / 'dsh-desktop' / 'weights')
runtime = DesktopRuntime(config, home=home)
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
    def open_popup():
        page = first.handle[0]
        with page.expect_popup() as opened:
            page.evaluate("url => window.open(url, 'owned-popup')", url)
        opened.value.wait_for_load_state('domcontentloaded')
    runtime.browser.call(open_popup)
    discovered = [row for row in runtime.target_directory('a') if row['kind'] == 'browser'
                  and row.get('url') == url and row.get('frame_bounds') is None]
    assert len(discovered) == 2, discovered
    popup = next(row for row in discovered if row['target_id'] != first.target_id)
    assert not any(row.get('tab_id') == popup['tab_id'] for row in runtime.target_directory('b'))
    report['popup_discovery'] = {'same_url_tabs': len(discovered), 'other_owner_adoptions': 0}
    runtime.browser.close_target('a', popup['target_id'])
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

    if '--observations' in sys.argv:
        service = runtime.observations
        long_id = 'locator_' + 'x' * 200
        def prepare_semantics():
            first.handle[1].evaluate('''longId => {
              const form = document.createElement('div');
              form.innerHTML = `<label for="${longId}">中文备注</label><input id="${longId}" value="初始值">
                <label><input id="check" type="checkbox" checked>加急</label>
                <select aria-label="城市"><option>杭州</option><option selected>上海</option></select>
                <input type="password" aria-label="密码" value="hidden-fixture-secret">
                <div role="dialog"><button id="dialog-button">确认弹窗</button></div>`;
              for (let i = 0; i < 100; i++) {
                const button = document.createElement('button'); button.textContent = '项目' + i; form.append(button);
              }
              const body = document.createElement('p'); body.textContent = '中文正文内容'.repeat(1800); form.append(body);
              document.body.prepend(form);
            }''', long_id)
        runtime.browser.call(prepare_semantics)
        observed = service.observe('a', first.target_id)['observation']
        assert observed['trace']['dom'] == 1 and observed['trace']['dom_frames'] == 3, observed
        assert observed['trace']['capture'] == 1 and observed['trace']['caption'] == 0
        assert observed['elements'][0]['name'] == '确认弹窗'
        oid = observed['observation_id']
        saved = service.get('a', oid)
        textbox = next(element for element in saved.elements if element.semantic.get('name') == '中文备注')
        assert len(textbox.locator['selector']) > 200
        assert next(element for element in saved.elements if element.semantic.get('name') == '加急').semantic['checked'] is True
        select = next(element for element in saved.elements if element.semantic.get('name') == '城市')
        assert select.semantic['options'] == ['杭州', '上海'] and select.semantic['selected'] == ['上海']
        password = next(element for element in saved.elements if element.semantic.get('name') == '密码')
        assert 'value' not in password.semantic
        def resolve_long():
            node = service.dom_element('a', textbox)
            try:
                assert node.input_value() == '初始值'
            finally:
                node.dispose()
        runtime.browser.call(resolve_long)
        all_refs = [element['ref'] for element in observed['elements']]
        body = observed['text']
        cursor = observed.get('next_cursor')
        pages = 1
        while cursor:
            page = service.observe('a', first.target_id, cursor=cursor)['observation']
            assert page['observation_id'] == oid and page['trace']['dom'] == 1
            assert page['frames'] == [] and all('bounds' not in element for element in page['elements'])
            all_refs.extend(element['ref'] for element in page['elements'])
            body += page['text']
            cursor = page.get('next_cursor')
            pages += 1
        assert len(all_refs) == len(set(all_refs)) == len(saved.elements)
        assert body == saved.text and len(body) > 6000
        fault('OBSERVATION_EXPIRED', lambda: service.observe('b', first.target_id, cursor=observed['next_cursor']))
        stable = service.observe('a', first.target_id)['observation']
        assert stable['frames'] == [] and stable['trace']['capture'] == 0, stable['trace']
        runtime.browser.call(lambda: first.handle[1].evaluate('id => document.getElementById(id).replaceWith(document.getElementById(id).cloneNode(true))', long_id))
        fault('REF_STALE', lambda: runtime.browser.call(service.dom_element, 'a', textbox))
        assert service.get('a', oid).text == body
        directed = service.observe('a', first.target_id, query='中文备注', image=False)['observation']
        assert len(directed['elements']) == 1 and directed['trace']['dom'] == 1 and directed['trace']['ocr'] == 0
        fresh = service.observe('a', first.target_id, image=True)['observation']
        roi = service.observe('a', first.target_id, region={'frame_id': fresh['frames'][0]['frame_id'],
                              'x': 0, 'y': 0, 'width': 300, 'height': 250}, image=True)['observation']
        assert roi['trace']['dom'] == 1 and roi['trace']['capture'] == 1 and roi['trace']['caption'] == 0
        assert roi['frames'][0]['width'] > 300
        assert service.image('a', roi['frames'][0]['frame_id'])[:2] == b'\xff\xd8'
        fault('FRAME_EXPIRED', lambda: service.image('b', roi['frames'][0]['frame_id']))
        native_observation = service.observe('b', native_b.target_id)['observation']
        assert native_observation['trace']['uia'] == 1 and native_observation['elements'], native_observation
        native_ref = service.get('b', native_observation['observation_id']).elements[0]
        service.uia.call(service.validate_uia, 'b', native_ref)
        previous_title = win32gui.GetWindowText(hwnd)
        win32gui.SetWindowText(hwnd, previous_title + ' changed')
        fault('REF_STALE', lambda: service.uia.call(service.validate_uia, 'b', native_ref))
        win32gui.SetWindowText(hwnd, previous_title)
        report['observations'] = {'dom_scans': observed['trace']['dom'], 'dom_frames': observed['trace']['dom_frames'],
                                   'elements': len(saved.elements), 'text_chars': len(body), 'pages': pages,
                                   'stable_capture_count': stable['trace']['capture'], 'roi': roi['trace'],
                                   'uia': native_observation['trace'], 'native_stale_ref_rejected': 1,
                                   'long_locator_chars': len(textbox.locator['selector'])}
        if '--vision' in sys.argv:
            runtime.browser.call(lambda: first.handle[1].evaluate('''() => {
              const canvas = document.createElement('canvas'); canvas.width = 500; canvas.height = 160;
              document.body.prepend(canvas);
              const context = canvas.getContext('2d'); context.fillStyle = 'white'; context.fillRect(0, 0, 500, 160);
              context.font = '24px Microsoft YaHei'; context.fillStyle = 'black'; context.fillText('中文小字 A7B9', 30, 80);
            }'''))
            visual = service.observe('a', first.target_id, query='A7B9', image=True)['observation']
            assert any('A7B9' in element['name'] for element in visual['elements']), visual
            assert visual['trace']['dom'] == 1 and visual['trace']['ocr'] == 1 and visual['trace']['yolo'] == 0
            report['visual_query'] = {'elements': visual['elements'], 'trace': visual['trace']}

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
    if '--migration' in sys.argv:
        runtime.browser.shutdown()
        legacy_profile = home / Path(profile).name
        shutil.copytree(profile, legacy_profile)
        reopened = runtime.open_target('b', url=url, call_id='legacy-copy')
        assert runtime.browser.profile == legacy_profile
        runtime.browser.call(check_state)
        runtime.browser.shutdown()
        legacy_profile.rename(home / (legacy_profile.name + '-backup'))
        reopened = runtime.open_target('b', url=url, call_id='rollback-original')
        assert str(runtime.browser.profile) == profile
        runtime.browser.call(check_state)
        report['profile_migration'] = {'legacy_copy_restored': 1, 'original_rollback_restored': 1,
                                       'cookie_checks': 2, 'site_setting_checks': 2, 'original_retained': 1}
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
from dsh_desktop.browser_support import _edge_executable
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
