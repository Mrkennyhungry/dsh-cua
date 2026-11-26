"""Owned interactive fixtures for unified P4 dispatch, readback and recovery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor

if os.environ.get('DSH_TEST_PACKAGES'):
    import site
    packages = Path(os.environ['DSH_TEST_PACKAGES'])
    site.addsitedir(str(packages))
    _dll_directory = os.add_dll_directory(str(packages / 'pywin32_system32'))

import win32api
import win32con
import win32gui


def child_window(unique, event_log):
    replacement_hwnd = None
    popup_hwnd = None
    def popup_procedure(hwnd, message, wparam, lparam):
        nonlocal popup_hwnd
        if message == win32con.WM_COMMAND and (wparam & 65535) == 110:
            owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
            win32gui.SetWindowText(win32gui.GetDlgItem(owner, 104), 'Owned popup selected')
            win32gui.DestroyWindow(hwnd)
            popup_hwnd = None
            return 0
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    def procedure(hwnd, message, wparam, lparam):
        nonlocal replacement_hwnd, popup_hwnd
        if message == win32con.WM_COMMAND and (wparam & 65535) == 109:
            if popup_hwnd is not None:
                win32gui.DestroyWindow(popup_hwnd)
                popup_hwnd = None
            else:
                left, top, _, _ = win32gui.GetWindowRect(win32gui.GetDlgItem(hwnd, 109))
                popup_hwnd = win32gui.CreateWindowEx(win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE,
                    popup_name, '', win32con.WS_POPUP | win32con.WS_BORDER,
                    left, top + 32, 195, 65, hwnd, 0, window.hInstance, None)
                win32gui.CreateWindow('Button', 'Owned menu item', win32con.WS_CHILD | win32con.WS_VISIBLE,
                    5, 5, 180, 45, popup_hwnd, 110, window.hInstance, None)
                win32gui.ShowWindow(popup_hwnd, win32con.SW_SHOWNOACTIVATE)
            return 0
        if message == win32con.WM_COMMAND and (wparam & 65535) == 107:
            replacement_hwnd = win32gui.CreateWindow(name, 'P4 actions ' + unique, win32con.WS_OVERLAPPEDWINDOW,
                120, 120, 620, 350, 0, 0, window.hInstance, None)
            win32gui.CreateWindow('Static', 'Replacement window text', win32con.WS_CHILD | win32con.WS_VISIBLE,
                20, 20, 400, 32, replacement_hwnd, 108, window.hInstance, None)
            win32gui.ShowWindow(replacement_hwnd, win32con.SW_SHOWNORMAL)
            win32gui.DestroyWindow(hwnd)
            return 0
        if message == win32con.WM_COMMAND and (wparam & 65535) == 103:
            value = win32gui.GetWindowText(win32gui.GetDlgItem(hwnd, 100))
            win32gui.SetWindowText(win32gui.GetDlgItem(hwnd, 104), 'Saved ' + value)
            with open(event_log, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps({'saved': value}) + '\n')
            return 0
        if message == win32con.WM_COMMAND and (wparam & 65535) == 105:
            win32gui.SetWindowText(win32gui.GetDlgItem(hwnd, 106), 'Native transient saved')
            threading.Timer(0.6, lambda: win32gui.PostMessage(hwnd, win32con.WM_APP + 18, 0, 0)).start()
            return 0
        if message == win32con.WM_APP + 18:
            win32gui.SetWindowText(win32gui.GetDlgItem(hwnd, 106), 'Native transient idle')
            return 0
        if message == win32con.WM_CLOSE:
            win32gui.MessageBox(hwnd, 'Preserved fixture changes', 'P4 save prompt ' + unique, win32con.MB_OKCANCEL)
            return 0
        if message == win32con.WM_APP + 17:
            win32gui.DestroyWindow(hwnd)
            return 0
        if message == win32con.WM_DESTROY:
            if replacement_hwnd is None or hwnd == replacement_hwnd:
                win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)
    window = win32gui.WNDCLASS()
    window.hInstance = win32api.GetModuleHandle(None)
    window.lpszClassName = 'P4Fixture' + unique
    window.lpfnWndProc = procedure
    name = win32gui.RegisterClass(window)
    popup_class = win32gui.WNDCLASS()
    popup_class.hInstance = window.hInstance
    popup_class.lpszClassName = 'P4Popup' + unique
    popup_class.lpfnWndProc = popup_procedure
    popup_name = win32gui.RegisterClass(popup_class)
    hwnd = win32gui.CreateWindow(name, 'P4 actions ' + unique, win32con.WS_OVERLAPPEDWINDOW,
                                 120, 120, 620, 350, 0, 0, window.hInstance, None)
    style = win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_TABSTOP
    win32gui.CreateWindow('Edit', 'initial', style | win32con.WS_BORDER | win32con.ES_AUTOHSCROLL,
                          20, 20, 240, 28, hwnd, 100, window.hInstance, None)
    win32gui.CreateWindow('Button', 'Urgent', style | win32con.BS_AUTOCHECKBOX,
                          20, 65, 150, 28, hwnd, 101, window.hInstance, None)
    combo = win32gui.CreateWindow('ComboBox', '', style | win32con.CBS_DROPDOWNLIST,
                                  20, 105, 200, 180, hwnd, 102, window.hInstance, None)
    for label in ('Hangzhou', 'Shanghai'):
        win32gui.SendMessage(combo, win32con.CB_ADDSTRING, 0, label)
    win32gui.SendMessage(combo, win32con.CB_SETCURSEL, 0, 0)
    win32gui.CreateWindow('Button', 'Save fixture', style, 20, 155, 160, 32, hwnd, 103, window.hInstance, None)
    win32gui.CreateWindow('Static', 'Pending', win32con.WS_CHILD | win32con.WS_VISIBLE,
                          20, 210, 500, 32, hwnd, 104, window.hInstance, None)
    win32gui.CreateWindow('Button', 'Show native toast', style, 300, 155, 180, 32, hwnd, 105, window.hInstance, None)
    win32gui.CreateWindow('Static', 'Native transient idle', win32con.WS_CHILD | win32con.WS_VISIBLE,
                          300, 195, 220, 32, hwnd, 106, window.hInstance, None)
    win32gui.CreateWindow('Button', 'Recreate target', style, 300, 240, 180, 32, hwnd, 107, window.hInstance, None)
    win32gui.CreateWindow('Button', 'Show owned menu', style, 300, 65, 180, 32, hwnd, 109, window.hInstance, None)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
    win32gui.PumpMessages()


if len(sys.argv) > 1 and sys.argv[1] == '--child':
    child_window(sys.argv[2], sys.argv[3])
    sys.exit(0)

from dsh_desktop.dpi import set_per_monitor_v2
set_per_monitor_v2()
from dsh_desktop import input_gate
from dsh_desktop.runtime import DesktopRuntime, RuntimeConfig
from dsh_desktop.targets import RuntimeFault


HTML = '''<!doctype html><meta charset="utf-8"><title>P4 form</title>
<label>备注<input id="text" value="initial"></label>
<label>加急<input id="check" type="checkbox"></label>
<select aria-label="城市" id="city"><option>杭州</option><option>上海</option></select>
<label>附件<input id="file" type="file"></label>
<button id="click" onclick="count++; document.querySelector('output').textContent='saved '+count"
 oncontextmenu="event.preventDefault(); right++;">保存</button>
<button id="hover" onmouseover="hovered++">悬停</button><output>pending</output>
<button id="toast" onclick="const el=document.createElement('div');el.textContent='Browser transient saved';document.body.append(el);setTimeout(()=>el.remove(),120)">短提示</button>
<button id="modal" onclick="document.querySelector('dialog').showModal()">弹窗</button>
<dialog><p>确认内容</p><button onclick="this.parentElement.close()">关闭弹窗</button></dialog>
<button id="nav" onclick="location.href='/next'">导航</button>
<button id="jsdialog" onclick="window.confirmResult=confirm('保留浏览器确认')">浏览器确认</button>
<button id="local-popup" style="position:absolute;left:700px;top:300px"
 onclick="document.querySelector('#local-menu').hidden=!document.querySelector('#local-menu').hidden">局部菜单</button>
<div id="local-menu" hidden style="position:absolute;left:700px;top:335px;width:160px;height:70px;background:#eee;color:#444">Popup item one<br>Popup item two</div>
<iframe title="embedded" srcdoc="&lt;button onclick=&quot;this.textContent='inner done'&quot;&gt;frame action&lt;/button&gt;"></iframe>
<div id="drag" style="width:240px;height:60px;background:#ccd" onpointerdown="down++" onpointerup="up++">拖动区域</div>
<div style="height:1800px"></div>
<script>window.count=0;window.right=0;window.hovered=0;window.down=0;window.up=0;</script>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        raw = (HTML if self.path == '/' else '<!doctype html><p>next document</p>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


home = Path(sys.argv[1]).resolve()
home.mkdir(parents=True, exist_ok=True)
os.environ['DSH_DESKTOP_HOME'] = str(home)
os.environ['DSH_DESKTOP_STATE_DIR'] = str(home / 'state')
runtime = DesktopRuntime(RuntimeConfig.from_home(home), home=home)
runtime.setup()
input_gate.clear_stop()
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
unique = uuid.uuid4().hex
child = subprocess.Popen([sys.executable, __file__, '--child', unique, str(home / 'native-events.jsonl')], creationflags=subprocess.CREATE_NO_WINDOW)
report = {'operations': [], 'models': 0, 'caption': 0}
started = time.monotonic()
sequence = 0


def observe(target):
    for attempt in range(3):
        try:
            return runtime.observations.observe('a', target.target_id)['observation']
        except RuntimeFault as error:
            if error.code != 'TARGET_CHANGED' or attempt == 2:
                raise
            time.sleep(0.1)


def ref(observed, name=None, role=None):
    matches = [element for element in observed['elements'] if (name is None or element['name'] == name)
               and (role is None or element['role'] == role)]
    assert len(matches) == 1, (name, role, matches, observed)
    return {'ref': matches[0]['ref']}


def act(target, observed, steps, **extra):
    global sequence
    sequence += 1
    arguments = {'target_id': target.target_id, 'observation_id': observed['observation_id'], 'steps': steps, **extra}
    call_started = time.perf_counter()
    result = runtime.act('a', 'probe-' + str(sequence), arguments)
    while result['status'] in ('accepted', 'running', 'cancelling'):
        result = runtime.operations.status('a', result['operation_id'], follow_ms=1000)
    report['operations'].append({'id': result['operation_id'], 'status': result['status'],
                                'steps': result['completed_steps'], 'dispatch': result['dispatch'],
                                'elapsed_ms': round((time.perf_counter() - call_started) * 1000, 3),
                                'response_bytes': len(json.dumps(result, ensure_ascii=False).encode())})
    assert result['success'], result
    assert result['status'] == 'completed', result
    return result, arguments


try:
    native = None
    for _ in range(100):
        hwnd = win32gui.FindWindow(None, 'P4 actions ' + unique)
        if hwnd:
            try:
                native = runtime.open_target('a', hwnd=hwnd)
                break
            except RuntimeFault as error:
                if error.code != 'TARGET_CLOSED':
                    raise
        time.sleep(0.1)
    assert native is not None, 'Owned fixture window did not become available'
    native_open = runtime.open('a', 'open-native', {'target_id': native.target_id, 'image': False})
    assert native_open['status'] == 'completed' and native_open['target_id'] == native.target_id, native_open
    native_observed = native_open['observation']
    report['open_native_initial_observation'] = 1
    window_result, _ = act(native, native_observed, [
        {'action': 'window', 'op': 'activate'},
        {'action': 'window', 'op': 'topmost', 'on': True},
        {'action': 'window', 'op': 'highlight', 'on': True},
    ])
    assert win32gui.GetWindowLong(hwnd, -20) & 8
    window_result, _ = act(native, window_result['observation'], [
        {'action': 'window', 'op': 'highlight', 'on': False},
        {'action': 'window', 'op': 'topmost', 'on': False},
    ])
    assert not win32gui.GetWindowLong(hwnd, -20) & 8
    report['window_activate_topmost_highlight'] = 1
    native_observed = window_result['observation']
    result, _ = act(native, native_observed, [
        {'action': 'input', 'target': ref(native_observed, role='textbox'), 'text': '中文内容', 'mode': 'replace'},
        {'action': 'check', 'target': ref(native_observed, 'Urgent'), 'checked': True},
        {'action': 'select', 'target': ref(native_observed, role='combobox'), 'values': ['Shanghai']},
        {'action': 'click', 'target': ref(native_observed, 'Save fixture')},
    ])
    assert next(element['value'] for element in result['observation']['elements'] if element['role'] == 'textbox') == '中文内容'
    assert win32gui.SendMessage(win32gui.GetDlgItem(hwnd, 101), win32con.BM_GETCHECK, 0, 0) == 1
    assert win32gui.SendMessage(win32gui.GetDlgItem(hwnd, 102), win32con.CB_GETCURSEL, 0, 0) == 1
    assert 'Saved 中文内容' in result['observation']['text'], result
    assert set(result['click_region_frames']) == {'before', 'after'}, result
    for crop in result['click_region_frames'].values():
        assert crop['width'] <= 400 and crop['height'] <= 400, crop
        assert runtime.observations.image('a', crop['frame_id'])
    report['click_region_before_after'] = 1
    observed = observe(native)
    toast, _ = act(native, observed, [{'action': 'click', 'target': ref(observed, 'Show native toast')}],
                   expect={'kind': 'text', 'text': 'Native transient saved', 'transient': True, 'timeout_ms': 3000})
    native_status = runtime.operations.status('a', toast['operation_id'])
    assert toast['expectation']['status'] == native_status['expectation']['status'] == 'met', toast
    assert toast['expectation']['evidence']['source'] == 'uia', toast
    report['native_transient'] = toast['expectation']
    menu, _ = act(native, observe(native), [{'action': 'click', 'target': ref(observe(native), 'Show owned menu')}],
                  expect={'kind': 'text', 'text': 'Owned menu item', 'transient': True, 'timeout_ms': 1000})
    candidate = next(item for item in menu['popup_candidates'] if ''.join(item['raw_text'].split()) == 'Ownedmenuitem')
    assert menu['click_region_frames']['after']['method'] == 'screen_region', menu
    assert candidate['bounds']['frame_id'] == menu['click_region_frames']['after']['frame_id'], menu
    selected, _ = act(native, {'observation_id': menu['popup_observation_id']},
                      [{'action': 'click', 'target': {'ref': candidate['ref']}}])
    assert 'Owned popup selected' in selected['observation']['text'], selected
    report['owned_popup_screen_capture_ocr_and_click'] = {'confidence': candidate['confidence'],
        'local_changed_ratio': menu['local_visual_effect_changed_ratio'], 'selected': True}
    report['native_form_steps'] = 4
    batch_observation = observe(native)
    batch, _ = act(native, batch_observation, [
        {'action': 'input', 'target': ref(batch_observation, role='textbox'), 'text': 'batch input', 'mode': 'replace'},
        {'action': 'key', 'keys': ['END']},
    ])
    assert batch['completed_steps'] == [0, 1], batch
    assert next(element['value'] for element in batch['observation']['elements'] if element['role'] == 'textbox') == 'batch input', batch
    report['native_input_key_batch'] = 1
    native_b = runtime.open_target('b', hwnd=hwnd)
    first_observation = observe(native)
    second_observation = runtime.observations.observe('b', native_b.target_id)['observation']
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = []
        for owner, selected, snapshot in [('a', native, first_observation), ('b', native_b, second_observation)]:
            requests.append(pool.submit(runtime.act, owner, 'concurrent-' + owner,
                {'target_id': selected.target_id, 'observation_id': snapshot['observation_id'], 'steps': [
                    {'action': 'input', 'target': ref(snapshot, role='textbox'), 'text': owner, 'mode': 'replace'},
                    {'action': 'click', 'target': ref(snapshot, 'Save fixture')}]}))
        concurrent = [request.result() for request in requests]
        assert all(result['status'] == 'completed' for result in concurrent), concurrent
    logs = [json.loads(line)['saved'] for line in (home / 'native-events.jsonl').read_text(encoding='utf-8').splitlines()]
    assert sorted(logs[-2:]) == ['a', 'b'], logs
    report['concurrent_native_owner_values'] = logs[-2:]
    native_image = runtime.observations.observe('a', native.target_id, image=True)['observation']
    snapshot = runtime.observations.get('a', native_image['observation_id'])
    edit = next(element for element in snapshot.elements if element.semantic['role'] == 'textbox')
    x, y, width, height = edit.rect
    frame = snapshot.frames[0]
    px, py = frame.geometry.from_screen(x + width / 2, y + height / 2)
    point_input, _ = act(native, native_image, [{'action': 'input',
        'target': {'frame_id': frame.frame_id, 'x': px, 'y': py}, 'text': '视觉输入', 'mode': 'replace'}])
    assert next(element['value'] for element in point_input['observation']['elements'] if element['role'] == 'textbox') == '视觉输入'
    report['native_frame_point_input'] = 1
    result['observation'] = observe(native)
    result, _ = act(native, result['observation'], [{'action': 'key', 'keys': ['TAB']}])
    result, _ = act(native, result['observation'], [{'action': 'close'}, {'action': 'key', 'keys': ['ENTER']}])
    assert result['outcome'] == 'interrupted' and result['completed_steps'] == [0], result
    prompt = win32gui.FindWindow(None, 'P4 save prompt ' + unique)
    assert prompt and win32gui.IsWindow(hwnd)
    win32gui.PostMessage(prompt, win32con.WM_COMMAND, win32con.IDCANCEL, 0)
    report['native_modal_preserved'] = 1
    browser_args = {'url': f'http://127.0.0.1:{server.server_port}/'}
    opened = runtime.open('a', 'open-browser', browser_args)
    while opened['status'] in ('accepted', 'running'):
        opened = runtime.operations.status('a', opened['operation_id'], follow_ms=1000)
    assert opened['status'] == 'completed' and opened['outcome'] == 'opened', opened
    replay = runtime.open('a', 'open-browser', browser_args)
    assert replay['operation_id'] == opened['operation_id'] and replay['target_id'] == opened['target_id']
    target = runtime.resolve_target('a', opened['target_id'])
    observed = opened['observation']
    report['open_browser_initial_observation_and_replay'] = 1
    queried, _ = act(target, observed, [
        {'action': 'dom_query', 'selector': '#click'},
        {'action': 'extract', 'selector': 'button', 'limit': 2},
    ])
    assert queried['steps'][0]['method']['nodes'][0]['text'] == '保存', queried
    assert len(queried['steps'][1]['method']['nodes']) == 2, queried
    settled = runtime.wait('a', 'browser-settle', {'target_id': target.target_id,
        'condition': {'kind': 'settle', 'quiet_ms': 150}, 'timeout_ms': 3000})
    while settled['status'] in ('accepted', 'running'):
        settled = runtime.operations.status('a', settled['operation_id'], follow_ms=1000)
    assert settled['status'] == 'completed' and settled['expectation']['status'] == 'met', settled
    report['web_query_extract_settle'] = 1
    popup, _ = act(target, observe(target), [{'action': 'click', 'target': ref(observe(target), '局部菜单')}])
    assert runtime.browser.call(lambda: target.handle[1].locator('#local-menu').is_visible())
    assert popup['local_visual_effect'], popup
    assert set(popup['click_region_frames']) == {'before', 'after'}, popup
    report['local_popup'] = {'local_visual_effect': popup['local_visual_effect'],
        'local_changed_ratio': popup['local_visual_effect_changed_ratio'],
        'full_visual_effect': popup.get('visual_effect'),
        'full_changed_ratio': popup.get('visual_effect_changed_ratio')}
    for phase, crop in popup['click_region_frames'].items():
        (home / f'click-region-{phase}.jpg').write_bytes(runtime.observations.image('a', crop['frame_id']))
    popup_closed, _ = act(target, observe(target), [{'action': 'click', 'target': ref(observe(target), '局部菜单')}])
    assert not runtime.browser.call(lambda: target.handle[1].locator('#local-menu').is_visible())
    assert popup_closed['local_visual_effect'], popup_closed
    assert popup_closed.get('toggle'), popup_closed
    report['local_popup']['second_click_closed'] = True
    report['local_popup']['toggle_detected'] = True
    observed = observe(target)
    attachment = home / 'upload.txt'
    attachment.write_text('fixture attachment', encoding='utf-8')
    result, arguments = act(target, observed, [
        {'action': 'input', 'target': ref(observed, '备注'), 'text': '中文', 'mode': 'replace'},
        {'action': 'input', 'target': ref(observed, '备注'), 'text': '追加', 'mode': 'insert'},
        {'action': 'check', 'target': ref(observed, '加急'), 'checked': True},
        {'action': 'select', 'target': ref(observed, '城市'), 'values': ['上海']},
        {'action': 'click', 'target': ref(observed, '保存'), 'count': 2},
        {'action': 'click', 'target': ref(observed, '保存'), 'button': 'right'},
        {'action': 'hover', 'target': ref(observed, '悬停')},
        {'action': 'attach', 'target': ref(observed, '附件'), 'files': [str(attachment)]},
    ])
    def browser_state():
        return target.handle[1].evaluate('''() => ({text:document.querySelector('#text').value,checked:document.querySelector('#check').checked,
          selected:document.querySelector('#city').value,files:document.querySelector('#file').files.length,count,right,hovered})''')
    state = runtime.browser.call(browser_state)
    assert state['text'] == '中文追加' and state['checked'] and state['selected'] == '上海' and state['files'] == 1, state
    assert state['count'] == 2 and state['right'] == 1 and state['hovered'] >= 1, state
    replay = runtime.act('a', 'probe-' + str(sequence), arguments)
    assert replay['operation_id'] == result['operation_id']
    assert runtime.browser.call(browser_state) == state
    report['dom_form'] = state
    report['response_loss_replay_extra_dispatches'] = 0
    observed = observe(target)
    result, _ = act(target, observed, [{'action': 'click', 'target': ref(observed, 'frame action')}])
    assert runtime.browser.call(lambda: target.handle[0].frames[1].locator('button').inner_text()) == 'inner done'
    report['nested_frame_action'] = 1
    image_observation = runtime.observations.observe('a', target.target_id, image=True)['observation']
    snapshot = runtime.observations.get('a', image_observation['observation_id'])
    textbox = next(element for element in snapshot.elements if element.semantic['name'] == '备注')
    x, y, width, height = textbox.rect
    frame = snapshot.frames[0]
    px, py = frame.geometry.from_screen(x + width / 2, y + height / 2)
    result, _ = act(target, image_observation, [{'action': 'input', 'target': {'frame_id': frame.frame_id, 'x': px, 'y': py},
                                              'text': '图片定位输入', 'mode': 'replace'}])
    assert runtime.browser.call(browser_state)['text'] == '图片定位输入'
    report['browser_frame_point_input'] = 1
    observed = result['observation']
    checked, _ = act(target, observed, [{'action': 'check', 'target': ref(observed, '加急'), 'checked': True}])
    assert checked['dispatch'] == 'none' and checked['steps'][0]['method'] == 'already_satisfied'
    observed = checked['observation']
    result, _ = act(target, observed, [{'action': 'click', 'target': ref(observed, '保存')}],
                    expect={'kind': 'text', 'text': 'saved 3', 'timeout_ms': 1000})
    assert result['expectation']['status'] == 'met'
    report['expectation_samples'] = result['expectation']['samples']
    observed = observe(target)
    toast, _ = act(target, observed, [{'action': 'click', 'target': ref(observed, '短提示')}],
                   expect={'kind': 'text', 'text': 'Browser transient saved', 'transient': True, 'timeout_ms': 3000})
    browser_status = runtime.operations.status('a', toast['operation_id'])
    assert toast['expectation']['status'] == browser_status['expectation']['status'] == 'met', toast
    assert toast['expectation']['evidence']['source'] == 'dom', toast
    report['browser_transient'] = toast['expectation']
    observed = observe(target)
    cancelled_watch = runtime.act('a', 'cancel-transient', {'target_id': target.target_id,
        'observation_id': observed['observation_id'], 'steps': [{'action': 'hover', 'target': ref(observed, '悬停')}],
        'expect': {'kind': 'text', 'text': 'never-transient-fixture', 'transient': True, 'timeout_ms': 5000}}, budget_ms=0)
    deadline = time.monotonic() + 5
    while True:
        running_watch = runtime.operations.status('a', cancelled_watch['operation_id'])
        if running_watch['completed_steps']:
            break
        assert time.monotonic() < deadline, running_watch
        time.sleep(0.02)
    runtime.operations.stop('a', cancelled_watch['operation_id'])
    stopped_watch = runtime.operations.status('a', cancelled_watch['operation_id'], follow_ms=5000)
    assert stopped_watch['status'] == 'cancelled', stopped_watch
    remaining = runtime.browser.call(lambda: target.handle[1].evaluate("Object.keys(window).filter(k => k.startsWith('__dsh_watch_')).length"))
    assert remaining == 0, remaining
    report['transient_cancelled_and_cleaned'] = 1
    before = runtime.browser.call(browser_state)['count']
    observed = observe(target)
    timeout_args = {'target_id': target.target_id, 'observation_id': observed['observation_id'], 'steps': [
        {'action': 'click', 'target': ref(observed, '保存')},
        {'action': 'click', 'target': ref(observed, '保存')}],
        'expect': {'kind': 'text', 'text': 'never-present-fixture', 'timeout_ms': 200, 'after_step': 0}}
    timeout_result = runtime.act('a', 'expect-timeout', timeout_args)
    assert timeout_result['status'] == 'completed' and timeout_result['verification']['status'] == 'unknown', timeout_result
    assert timeout_result['completed_steps'] == [0] and timeout_result['stopped_at'] == 1
    assert runtime.browser.call(browser_state)['count'] == before + 1
    report['expect_timeout_completed_steps'] = timeout_result['completed_steps']
    native_snapshot = observe(native)
    waiting = runtime.act('a', 'wait-releases-input', {'target_id': native.target_id,
        'observation_id': native_snapshot['observation_id'], 'steps': [{'action': 'key', 'keys': ['TAB']}],
        'expect': {'kind': 'text', 'text': 'never-present-fixture', 'timeout_ms': 2000}}, budget_ms=0)
    deadline = time.monotonic() + 2
    while runtime.operations.status('a', waiting['operation_id'])['expectation']['status'] != 'pending':
        assert time.monotonic() < deadline
        time.sleep(0.01)
    before = runtime.browser.call(browser_state)['count']
    result, _ = act(target, observe(target), [{'action': 'click', 'target': ref(observe(target), '保存')}])
    assert runtime.browser.call(browser_state)['count'] == before + 1
    assert runtime.operations.status('a', waiting['operation_id'])['status'] == 'running'
    runtime.operations.stop('a', waiting['operation_id'])
    cancelled = runtime.operations.status('a', waiting['operation_id'], follow_ms=3000)
    assert cancelled['status'] == 'cancelled' and cancelled['completed_steps'] == [0], cancelled
    report['input_available_while_waiting'] = 1
    report['cancelled_wait_completed_steps'] = cancelled['completed_steps']
    observed = observe(target)
    drag_rect = runtime.browser.call(lambda: target.handle[1].locator('#drag').bounding_box())
    image_observation = runtime.observations.observe('a', target.target_id, image=True)['observation']
    frame = runtime.frames.get('a', image_observation['frames'][0]['frame_id'], runtime.resolve_target('a', target.target_id))
    first_point = frame.geometry.from_screen(drag_rect['x'] + 20, drag_rect['y'] + 20)
    second_point = frame.geometry.from_screen(drag_rect['x'] + 200, drag_rect['y'] + 20)
    def point(value):
        return {'frame_id': frame.frame_id, 'x': value[0], 'y': value[1]}
    result, _ = act(target, image_observation, [{'action': 'drag', 'from': point(first_point), 'to': point(second_point)}])
    counts = runtime.browser.call(lambda: target.handle[1].evaluate('({down,up})'))
    assert counts == {'down': 1, 'up': 1}, counts
    report['frame_drag'] = counts
    observed = observe(target)
    result, _ = act(target, observed, [{'action': 'scroll', 'target': ref(observed, '悬停'), 'direction': 'down', 'amount': 3}])
    # Wheel delivery is asynchronous in Chromium.
    runtime.browser.call(lambda: target.handle[0].wait_for_timeout(150))
    assert runtime.browser.call(lambda: target.handle[1].evaluate('scrollY')) > 0
    report['scroll_ticks'] = 3
    runtime.browser.call(lambda: target.handle[1].evaluate('scrollTo(0,0)'))
    observed = observe(target)
    runtime.browser.call(lambda: target.handle[1].evaluate("document.querySelector('#hover').replaceWith(document.querySelector('#hover').cloneNode(true))"))
    stale = runtime.act('a', 'stale', {'target_id': target.target_id, 'observation_id': observed['observation_id'],
                                     'steps': [{'action': 'click', 'target': ref(observed, '悬停')}]})
    assert stale['status'] == 'failed' and stale['dispatch'] == 'none' and 'observation' in stale, stale
    report['stale_ref_rejected'] = 1
    observed = observe(target)
    result, _ = act(target, observed, [{'action': 'click', 'target': ref(observed, '弹窗')},
                                     {'action': 'click', 'target': ref(observed, '保存')}])
    assert result['outcome'] == 'interrupted' and result['completed_steps'] == [0], result
    runtime.browser.call(lambda: target.handle[1].evaluate('document.querySelector("dialog").close()'))
    report['dom_modal_stopped_at'] = result['stopped_at']
    observed = observe(target)
    result, _ = act(target, observed, [{'action': 'click', 'target': ref(observed, '浏览器确认')},
                                     {'action': 'click', 'target': ref(observed, '保存')}])
    assert result['completed_steps'] == [0] and result['outcome'] == 'interrupted', result
    assert result['observation']['dialog']['message'] == '保留浏览器确认', result
    assert runtime.browser.call(lambda: bool(runtime.browser.pending_dialogs))
    result, _ = act(target, result['observation'], [{'action': 'key', 'keys': ['ESCAPE']}])
    assert runtime.browser.call(lambda: target.handle[1].evaluate('window.confirmResult')) is False
    assert not runtime.browser.call(lambda: bool(runtime.browser.pending_dialogs))
    report['browser_dialog_preserved_and_dismissed_explicitly'] = 1
    observed = observe(target)
    result, _ = act(target, observed, [{'action': 'click', 'target': ref(observed, '导航')},
                                     {'action': 'click', 'target': ref(observed, '保存')}])
    assert result['outcome'] == 'interrupted' and result['completed_steps'] == [0], result
    assert 'next document' in result['observation']['text']
    report['navigation_stopped_at'] = result['stopped_at']
    observed = result['observation']
    result, _ = act(target, observed, [{'action': 'close'}])
    assert result.get('closed'), result
    report['tab_closed'] = 1
    cleanup, _ = act(native, observe(native), [{'action': 'window', 'op': 'cleanup'}])
    assert cleanup['binding_released'] and win32gui.IsWindow(hwnd), cleanup
    assert not any(identity[0] == 'a' for identity in runtime._window_effects)
    report['window_cleanup_preserves_application'] = 1
    native = runtime.open_target('a', hwnd=hwnd)
    old_observation = observe(native)
    rebound, _ = act(native, old_observation, [{'action': 'click', 'target': ref(old_observation, 'Recreate target')}])
    assert rebound.get('rebound') and rebound.get('closed'), rebound
    assert rebound['target_id'] != native.target_id, rebound
    assert rebound['observation']['rebound_from'] == native.target_id, rebound
    assert 'Replacement window text' in rebound['observation']['text'], rebound
    stale_target = runtime.act('a', 'old-target-observation', {'target_id': rebound['target_id'],
        'observation_id': old_observation['observation_id'], 'steps': [{'action': 'key', 'keys': ['TAB']}]})
    assert stale_target['status'] == 'failed' and stale_target['error']['code'] == 'TARGET_STALE', stale_target
    report['native_target_rebind_and_stale_guard'] = 1
    report['parser_initializations'] = runtime.parser_initializations
    report['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
except BaseException:
    import traceback
    (home / 'failure.txt').write_text(traceback.format_exc(), encoding='utf-8')
    (home / 'partial-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    raise
finally:
    prompt = win32gui.FindWindow(None, 'P4 save prompt ' + unique)
    if prompt:
        win32gui.PostMessage(prompt, win32con.WM_COMMAND, win32con.IDCANCEL, 0)
    hwnd = win32gui.FindWindow(None, 'P4 actions ' + unique)
    if hwnd:
        win32gui.PostMessage(hwnd, win32con.WM_APP + 17, 0, 0)
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.terminate()
        child.wait(timeout=5)
    runtime.shutdown()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
(home / 'actions-probe.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))
