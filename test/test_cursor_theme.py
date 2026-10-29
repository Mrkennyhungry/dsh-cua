import importlib.util
import unittest
from pathlib import Path
import numpy as np
p=Path(__file__).resolve().parents[1]/'kernel/dsh_desktop/cursor_theme.py'
spec=importlib.util.spec_from_file_location('cursor_theme',p)
theme=importlib.util.module_from_spec(spec);spec.loader.exec_module(theme)
class CursorThemesTest(unittest.TestCase):
    def test_all_states_have_stable_hotspot_and_size(self):
        for palette in theme.PALETTES:
            for action in theme.ACTIONS:
                for frame in range(4):
                    a=theme.render(action=action,frame=frame,palette=palette)
                    self.assertEqual(a.shape,(48,48,4))
                    self.assertEqual(a.dtype,np.uint8)
                    self.assertEqual(a[0,0,3],255)
                    self.assertGreater(np.count_nonzero(a[:,:,3]),150)
    def test_action_frames_and_palettes_differ(self):
        self.assertFalse(np.array_equal(theme.render(action='click',frame=0),theme.render(action='click',frame=1)))
        self.assertFalse(np.array_equal(theme.render(palette='ocean'),theme.render(palette='mint')))
        self.assertFalse(np.array_equal(theme.render(action='idle'),theme.render(action='type')))
    def test_plugin_and_validation(self):
        theme.register_theme('test',lambda *_:np.zeros((2,2,4),dtype=np.uint8))
        self.assertEqual(theme.render('test',scale=2).shape,(4,4,4))
        for kw in ({'theme':'missing'},{'action':'missing'},{'palette':'missing'},{'scale':3}):
            with self.assertRaises(ValueError):theme.render(**kw)
        theme.register_theme('bad',lambda *_:np.zeros((2,2),dtype=np.uint8))
        with self.assertRaises(ValueError):theme.render('bad')
    def test_load_theme_dir_registers_only_valid_plugins(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            Path(d,'demo.py').write_text(
                "import numpy as np\n"
                "def render(action,frame,palette):\n"
                "    return np.full((2,2,4),9,dtype=np.uint8)\n",encoding='utf-8')
            Path(d,'broken.py').write_text("raise RuntimeError('boom')\n",encoding='utf-8')
            Path(d,'_skip.py').write_text("raise RuntimeError('skipped')\n",encoding='utf-8')
            self.assertEqual(theme.load_theme_dir(d),('demo',))
            self.assertIn('demo',theme.themes())
            self.assertEqual(theme.render('demo').shape,(2,2,4))
            self.assertNotIn('broken',theme.themes())
if __name__=='__main__':unittest.main()
