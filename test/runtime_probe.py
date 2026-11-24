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
             'onnxruntime', 'rapidocr_onnxruntime', 'anthropic',
             'dsh_desktop.standalone_bridge', 'dsh_desktop.legacy_bridge',
             'dsh_desktop.eternity', 'dsh_desktop.controller', 'dsh_desktop.server',
             'dsh_desktop.web_tools', 'dsh_desktop.decide', 'dsh_desktop.deepseek_caption'}


class MissingOptionalDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in FORBIDDEN or fullname.split('.')[0] in FORBIDDEN:
            raise ModuleNotFoundError(f'Optional dependency unavailable in runtime probe: {fullname}')


sys.meta_path.insert(0, MissingOptionalDependencies())
home = Path(sys.argv[1]).resolve()
os.environ['DSH_DESKTOP_HOME'] = str(home)
os.environ['DSH_DESKTOP_STATE_DIR'] = str(home / 'state')
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
    def operation(kind, arguments):
        response = client.post('/api/dsh/runtime/' + kind,
                               json={'request_key': uuid.uuid4().hex, 'arguments': arguments})
        assert response.status_code == 200, response.text
        value = response.json()
        while value.get('status') in ('queued', 'running', 'cancelling'):
            response = client.post('/api/dsh/runtime/status',
                                   json={'operation_id': value['operation_id'], 'follow_ms': 5000})
            assert response.status_code == 200, response.text
            value = response.json()
        assert value.get('success') and value.get('status') == 'completed', value
        return value

    health = client.get('/api/dsh/health')
    assert health.status_code == 200, health.text
    assert health.json()['parser_initializations'] == 0
    assert client.get('/api/dsh/health', headers={'Origin': 'https://example.invalid'}).status_code == 403
    assert client.get('/api/dsh/health', headers={'Authorization': 'Bearer invalid'}).status_code == 401
    retired = ['/tasks', '/windows', '/web', '/kb', '/decide']
    for path in retired:
        assert client.post('/api/dsh' + path, json={}).status_code == 404
    directory = operation('observe', {})
    status = client.post('/api/dsh/runtime/status', json={})
    assert status.status_code == 200 and status.json()['success'], status.text
    report = {'protocol': health.json()['protocol'], 'parser_initializations': 0,
              'forbidden_imports_loaded': len(FORBIDDEN.intersection(sys.modules)),
              'windows': len([row for row in directory['targets'] if row['kind'] == 'window']),
              'retired_routes_absent': len(retired),
              'health_ms': round((time.monotonic() - start) * 1000, 3)}
    if '--live' in sys.argv:
        title = uuid.uuid4().hex
        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('test_cleanup_dialog_live.py')), '--child', title],
                                 creationflags=subprocess.CREATE_NO_WINDOW, stderr=subprocess.PIPE)
        try:
            window = None
            for _ in range(50):
                rows = operation('observe', {})['targets']
                window = next((row for row in rows if title in row['title']), None)
                if window:
                    break
                time.sleep(0.1)
            assert window, ('Owned fixture window did not appear', child.poll(),
                            child.stderr.read().decode(errors='replace') if child.poll() is not None else '')
            shot = operation('open', {'target_id': window['target_id'], 'image': True})['observation']
            captured = client.get('/api/dsh/runtime/frames/' + shot['frames'][0]['frame_id'])
            assert captured.status_code == 200, captured.text
            from PIL import Image
            from io import BytesIO
            with Image.open(BytesIO(captured.content)) as image:
                report['capture'] = {'width': image.width, 'height': image.height, 'format': image.format}
            assert shot['elements'], shot
            report['uia_controls'] = len(shot['elements'])
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
            opened = operation('open', {'url': f'http://127.0.0.1:{server.server_port}', 'image': True})
            dom = operation('observe', {'target_id': opened['target_id'], 'image': False})['observation']
            assert 'Runtime DOM fixture' in dom['text'], dom
            report['dom_matches'] = len(dom['elements'])
            assert client.post('/api/dsh/runtime/stop', json={}).json()['success']
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
