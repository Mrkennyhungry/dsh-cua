import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ElevatedLogTests(unittest.TestCase):
    def test_wrapper_redirects_python_and_native_streams_to_configured_state_dir(self):
        root = Path(__file__).resolve().parent.parent
        scratch = root / '.handoff' / 'test-scratch'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            base = Path(tmp)
            pkg = base / 'src' / 'dsh_desktop'
            pkg.mkdir(parents=True)
            (pkg / '__init__.py').write_text('', encoding='utf-8')
            (pkg / 'dsh_bridge_cli.py').write_text(
                "import sys,os\nprint('中文输出')\nprint('错误日志', file=sys.stderr)\nos.write(1, b'native-out\\n')\nos.write(2, b'native-err\\n')\n",
                encoding='utf-8')
            env = os.environ.copy()
            env.update(DSH_DESKTOP_STATE_DIR=str(base / 'logs'), DSH_DESKTOP_HOME=str(base / 'home'),
                       DSH_DESKTOP_INSTALL=str(base / 'install'), DSH_DESKTOP_SOURCE=str(base / 'src'))
            result = subprocess.run([sys.executable, str(root / 'scripts' / 'run-kernel-high.py')],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
            stdout = (base / 'logs' / 'kernel.out.log').read_text(encoding='utf-8')
            stderr = (base / 'logs' / 'kernel.err.log').read_text(encoding='utf-8')
            self.assertIn('中文输出', stdout)
            self.assertIn('native-out', stdout)
            self.assertIn('错误日志', stderr)
            self.assertIn('native-err', stderr)


if __name__ == '__main__':
    unittest.main()
