import os
import runpy
import sys
from pathlib import Path

# A scheduled task has no DSH-owned stdout/stderr handles. Redirect both Python
# logging and native subprocess output to the same files reported by health.
state_dir = Path(os.environ.get('DSH_DESKTOP_STATE_DIR') or
                 (Path(os.environ['DSH_DESKTOP_HOME']) / 'state'))
state_dir.mkdir(parents=True, exist_ok=True)
_stdout_log = open(state_dir / 'kernel.out.log', 'a', encoding='utf-8', buffering=1, errors='backslashreplace')
_stderr_log = open(state_dir / 'kernel.err.log', 'a', encoding='utf-8', buffering=1, errors='backslashreplace')
for target, log in ((1, _stdout_log), (2, _stderr_log)):
    try:
        os.dup2(log.fileno(), target)
    except OSError:
        pass
sys.stdout, sys.stderr = _stdout_log, _stderr_log

install_root = Path(os.environ['DSH_DESKTOP_INSTALL'])
source_root = Path(os.environ['DSH_DESKTOP_SOURCE'])
venv = install_root / 'venv'
site = venv / 'Lib' / 'site-packages'
dll_dir = site / 'pywin32_system32'
_dll_directory = None
if dll_dir.is_dir():
    # Keep the handle alive until the kernel exits; dropping it closes the
    # directory immediately on CPython.
    _dll_directory = os.add_dll_directory(str(dll_dir))
for entry in (site, site / 'win32', site / 'win32' / 'lib', site / 'Pythonwin', source_root):
    if entry.is_dir():
        sys.path.insert(0, str(entry))
home = Path(os.environ['DSH_DESKTOP_HOME'])
port = os.environ.get('DSH_DESKTOP_PORT', '8766')
os.environ['HERMES_HOME'] = str(home)
sys.argv = ['dsh_desktop.dsh_bridge_cli', '--home-dir', str(home), '--port', port]
runpy.run_module('dsh_desktop.dsh_bridge_cli', run_name='__main__')
