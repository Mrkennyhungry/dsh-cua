"""Run the real runtime against owned fixtures with optional imports denied."""
import importlib.abc
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


FORBIDDEN = {'tools', 'run_agent', 'hermes_state', 'webview', 'croniter',
             'onnxruntime', 'rapidocr_onnxruntime', 'anthropic'}


class MissingOptionalDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in FORBIDDEN:
            raise ModuleNotFoundError(f'Optional dependency unavailable in runtime probe: {fullname}')


sys.meta_path.insert(0, MissingOptionalDependencies())
home = Path(sys.argv[1]).resolve()
os.environ['DSH_DESKTOP_HOME'] = str(home)
os.environ['DSH_DESKTOP_BRIDGE_TOKEN'] = uuid.uuid4().hex * 2
os.environ.pop('DEEPSEEK_API_KEY', None)
os.environ.pop('PARSER_SERVICE_URL', None)
os.environ.pop('PARSER_SERVICE_TOKEN', None)
with socket.socket() as reservation:
    reservation.bind(('127.0.0.1', 0))
    os.environ['DSH_DESKTOP_CDP_PORT'] = str(reservation.getsockname()[1])

from dsh_desktop.dsh_bridge_cli import create_app
from fastapi.testclient import TestClient

app = create_app(home, 18766)
headers = {'Authorization': 'Bearer ' + os.environ['DSH_DESKTOP_BRIDGE_TOKEN'], 'X-DSH-Owner': '1' * 64}
start = time.monotonic()
with TestClient(app, base_url='http://127.0.0.1:18766', headers=headers) as client:
    health = client.get('/api/dsh/health')
    assert health.status_code == 200, health.text
    assert health.json()['parser_initializations'] == 0
    assert client.get('/api/dsh/health', headers={'Origin': 'https://example.invalid'}).status_code == 403
    assert client.get('/api/dsh/health', headers={'Authorization': 'Bearer invalid'}).status_code == 401
    assert client.post('/api/dsh/tasks', json={'task': 'unused', 'request_id': 'p1'}).status_code == 503
    windows = client.get('/api/dsh/windows')
    assert windows.status_code == 200, windows.text
    report = {'protocol': health.json()['protocol'], 'parser_initializations': 0,
              'forbidden_imports_loaded': len(FORBIDDEN.intersection(sys.modules)),
              'windows': windows.json()['count'], 'health_ms': round((time.monotonic() - start) * 1000, 3)}
    if '--live' in sys.argv:
        title = uuid.uuid4().hex
        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('test_cleanup_dialog_live.py')), '--child', title],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            window = None
            for _ in range(50):
                rows = client.get('/api/dsh/windows').json()['windows']
                window = next((row for row in rows if title in row['title']), None)
                if window:
                    break
                time.sleep(0.1)
            assert window, 'Owned fixture window did not appear'
            bound = client.post('/api/dsh/windows/bind', json={'hwnd': window['hwnd']})
            assert bound.status_code == 200, bound.text
            window_id = bound.json()['window_id']
            captured = client.post(f'/api/dsh/windows/{window_id}/capture', json={})
            assert captured.status_code == 200 and captured.json().get('success') is not False, captured.text
            shot = captured.json()
            from PIL import Image
            with Image.open(shot['image_path']) as image:
                report['capture'] = {'width': image.width, 'height': image.height, 'format': image.format}
            uia = client.post(f'/api/dsh/windows/{window_id}/uia', json={'max_nodes': 50, 'max_depth': 4})
            assert uia.status_code == 200 and uia.json().get('control_count', 0) > 0, uia.text
            report['uia_controls'] = uia.json()['control_count']
            client.post(f'/api/dsh/windows/{window_id}/unbind', json={}).raise_for_status()
        finally:
            child.terminate()
            child.wait(timeout=5)

        class FixtureHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = b'<html><body><button id="p1">Runtime DOM fixture</button></body></html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            opened = client.post('/api/dsh/web', json={'action': 'open', 'url': f'http://127.0.0.1:{server.server_port}', 'timeout_ms': 15000})
            assert opened.status_code == 200 and opened.json().get('success'), opened.text
            dom = client.post('/api/dsh/web', json={'action': 'dom', 'selector': '#p1'})
            assert dom.status_code == 200 and 'Runtime DOM fixture' in dom.text, dom.text
            report['dom_matches'] = dom.json().get('count')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        assert app.state.runtime.parser_initializations == 0
        assert not FORBIDDEN.intersection(sys.modules)
        report['live_elapsed_ms'] = round((time.monotonic() - start) * 1000, 3)

    if '--companion' in sys.argv:
        from dsh_desktop import companion
        for attempt in range(2):
            response = client.post('/api/dsh/companion/start', json={'client_pid': os.getpid()})
            assert response.status_code == 200, response.text
            instance = companion.get_companion()
            overlay = instance._overlay
            process = instance._ui_process
            assert overlay is not None and not overlay.error, getattr(overlay, 'error', None)
            assert process is not None and process.poll() is None
            response = client.post('/api/dsh/companion/shutdown', json={})
            assert response.status_code == 200, response.text
            assert process.poll() is not None
            assert not overlay._thread.is_alive()
        report['companion_start_stop_cycles'] = 2

assert app.state.runtime._ui_parser is None
from dsh_desktop.desktop_tools import AppControllerRef
assert AppControllerRef.current is None
report['released_runtime'] = True
print(json.dumps(report))
