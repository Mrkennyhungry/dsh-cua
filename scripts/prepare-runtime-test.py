"""Build an isolated smoke-test environment from installed core distributions."""
from importlib import metadata
from pathlib import Path
import shutil
import sys
import venv

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

root = Path(__file__).resolve().parent.parent
environment = (root / '.runtime/runtime-test-venv').resolve()
venv.EnvBuilder().create(environment)
destination = environment / 'Lib/site-packages'
visited = set()
forbidden = {'hermes-agent', 'pywebview', 'croniter', 'onnxruntime', 'rapidocr-onnxruntime', 'anthropic'}


def copy_distribution(name):
    key = canonicalize_name(name)
    if key in visited:
        return
    if key in forbidden:
        raise RuntimeError(f'Core runtime dependency includes optional package {key}')
    distribution = metadata.distribution(name)
    visited.add(key)
    for entry in distribution.files or []:
        target = (destination / entry).resolve()
        if not target.is_relative_to(environment):
            raise RuntimeError(f'Distribution entry escapes the test environment: {entry}')
        source = Path(distribution.locate_file(entry))
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for value in distribution.requires or []:
        requirement = Requirement(value)
        if requirement.marker is None or requirement.marker.evaluate({'extra': ''}):
            copy_distribution(requirement.name)


# TestClient adds httpx2 to the runtime closure. All versions come from the
# installation under test, making this useful when public SDK wheels lag DSH.
for name in ['fastapi', 'uvicorn', 'opencv-python', 'numpy', 'psutil', 'pywin32',
             'pyyaml', 'pyautogui', 'pynput', 'mss', 'playwright', 'Pillow',
             'pywinauto', 'requests', 'httpx2']:
    copy_distribution(name)
print(f'{len(visited)} distributions copied using Python {sys.version.split()[0]}')
print(environment / 'Scripts/python.exe')
