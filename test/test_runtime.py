import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from dsh_desktop.runtime import DesktopRuntime, RuntimeConfig


class RuntimeTests(unittest.TestCase):
    def test_default_app_runs_without_optional_dependencies_or_key(self):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('runtime_probe.py')), home],
                                    text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(report['protocol'], 2)
        self.assertEqual(report['parser_initializations'], 0)
        self.assertEqual(report['forbidden_imports_loaded'], 0)
        self.assertTrue(report['released_runtime'])

    def test_runtime_config_reads_only_runtime_sections_and_resolves_home(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config.yaml').write_text('workspace:\n  screenshot_dir: shots\nparser:\n  engine: local-deepseek\nmodel:\n  api_key: unused\n', encoding='utf-8')
            config = RuntimeConfig.from_home(home)
            self.assertEqual(config.workspace.screenshot_dir, str(home / 'shots'))
            self.assertFalse(hasattr(config, 'model'))

    def test_parser_initializes_once_under_concurrent_access(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as directory:
            runtime = DesktopRuntime(RuntimeConfig.from_home(Path(directory)))
            fake = mock.Mock()
            with mock.patch.dict(sys.modules, {'dsh_desktop.ui_parser': mock.Mock(UIParser=fake)}):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    parsers = list(pool.map(lambda _: runtime.ui_parser, range(8)))
            self.assertEqual(runtime.parser_initializations, 1)
            fake.assert_called_once()
            self.assertTrue(all(parser is parsers[0] for parser in parsers))

    def test_ocr_and_yolo_initialize_independently_once(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = DesktopRuntime(RuntimeConfig.from_home(Path(directory)))
            parser = mock.Mock()
            with mock.patch.dict(sys.modules, {'dsh_desktop.ui_parser': mock.Mock(UIParser=parser)}):
                runtime.perceive(mock.Mock(), 'ocr')
                runtime.perceive(mock.Mock(), 'ocr')
                runtime.perceive(mock.Mock(), 'yolo')
            self.assertEqual(parser.call_count, 2)
            self.assertEqual(parser.call_args_list[0].kwargs, {'enable_ocr': True, 'enable_yolo': False})
            self.assertEqual(parser.call_args_list[1].kwargs, {'enable_ocr': False, 'enable_yolo': True})
            self.assertEqual(runtime.parser_initializations, 2)

    def test_companion_emergency_stop_calls_runtime_callback(self):
        from dsh_desktop.companion import Companion
        companion = Companion()
        companion._stop_all = mock.Mock()
        with mock.patch('dsh_desktop.input_gate.emergency_stop') as stop:
            companion.stop_everything()
        stop.assert_called_once()
        companion._stop_all.assert_called_once()

    def test_watch_shutdown_releases_all_sessions(self):
        from dsh_desktop.watch import WatchRegistry
        registry = WatchRegistry()
        sessions = [mock.Mock(), mock.Mock()]
        registry._sessions = dict(zip(['a', 'b'], sessions))
        registry.stop_all()
        self.assertEqual(registry._sessions, {})
        for session in sessions:
            session.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
