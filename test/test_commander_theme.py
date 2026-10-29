import unittest
import numpy as np
from dsh_desktop.mascot_theme import ACTIONS,render,WIDTH,HEIGHT,CAPTIONS

class CommanderArtTests(unittest.TestCase):
    def test_all_poses_and_bubbles_render_with_transparent_outer_margin(self):
        for action in ACTIONS:
            for frame in range(4):
                bitmap=render(action,frame)
                self.assertEqual(bitmap.shape,(HEIGHT,WIDTH,4))
                self.assertEqual(bitmap.dtype,np.uint8)
                self.assertEqual(bitmap[0,0,3],0)
                self.assertGreater(np.count_nonzero(bitmap[:,:,3]),1000)
    def test_entrance_thinking_and_cast_have_distinct_art(self):
        self.assertFalse(np.array_equal(render('enter'),render('thinking')))
        self.assertFalse(np.array_equal(render('thinking',0),render('thinking',1)))
        self.assertFalse(np.array_equal(render('click',0),render('click',1)))
    def test_right_click_has_its_own_caption(self):
        self.assertIn('右键',CAPTIONS['right_click'])
        self.assertNotEqual(CAPTIONS['right_click'],CAPTIONS['click'])
if __name__=='__main__':unittest.main()
