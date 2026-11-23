"""Run an owned live fixture with an existing interpreter's integrity level.

Windows lowers processes launched from executables carrying a Low Integrity
label. Reuse the installed interpreter and the isolated dependency directory
without changing either executable labels or the user's security settings.
"""
import os
from pathlib import Path
import runpy
import site
import sys

root = Path(__file__).resolve().parent.parent
packages = root / '.runtime' / 'runtime-test-venv' / 'Lib' / 'site-packages'
if not packages.is_dir():
    raise SystemExit('Run scripts/prepare-runtime-test.py first')
_dll_directory = os.add_dll_directory(str(packages / 'pywin32_system32'))
site.addsitedir(str(packages))
sys.path.insert(0, str(root / 'kernel'))
os.environ['DSH_TEST_PACKAGES'] = str(packages)
os.environ['PYTHONPATH'] = os.pathsep.join(sys.path)
sys.argv = sys.argv[1:]
if not sys.argv:
    raise SystemExit('Supply an owned probe script and its arguments')
runpy.run_path(sys.argv[0], run_name='__main__')
