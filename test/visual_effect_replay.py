"""Replay user-supplied JPEG evidence and isolate its measured button change."""
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np

from dsh_desktop.actions import ActionDispatcher
from dsh_desktop.geometry import FrameGeometry
from dsh_desktop.targets import Frame


directory = Path(sys.argv[1])
dispatcher = ActionDispatcher(None)
report = {'cases': {}}
for label, before_name, after_name in (
    ('qqmusic', '0.5.6-A_f32cbfd3_000854.jpg', '0.5.6-B_89b4d1ee_000900.jpg'),
    ('steam', '0.5.7-A_before_4d5d9c77.jpg', '0.5.7-B_after_9b67e35e.jpg'),
):
    before, after = [(directory / name).read_bytes() for name in (before_name, after_name)]
    regions = []
    if label == 'qqmusic':
        frame = Frame('replay', 'replay', 'replay', 1,
                      FrameGeometry((0, 0, 1226, 800), (1226, 800))).summary()
        regions = dispatcher._visual_regions(frame, {'point': (723, 742), 'rect': (703, 726, 40, 32)})
    result = dispatcher._compare_visual_effect(before, after, regions)
    assert result['visual_effect_global_changed'], result
    report['cases'][label] = {**result, 'before_sha256': hashlib.sha256(before).hexdigest(),
                              'after_sha256': hashlib.sha256(after).hexdigest()}
    if label == 'qqmusic':
        assert result['visual_effect_local_changed'], result
        before_pixels = cv2.imdecode(np.frombuffer(before, np.uint8), cv2.IMREAD_COLOR)
        after_pixels = cv2.imdecode(np.frombuffer(after, np.uint8), cv2.IMREAD_COLOR)
        isolated = before_pixels.copy()
        isolated[710:790, 680:780] = after_pixels[710:790, 680:780]
        # PNG preserves these decoded JPEG pixels while excluding the carousel.
        before_png = cv2.imencode('.png', before_pixels)[1].tobytes()
        isolated_png = cv2.imencode('.png', isolated)[1].tobytes()
        local = dispatcher._compare_visual_effect(before_png, isolated_png, regions)
        assert local['visual_effect'] and local['visual_effect_local_changed'], local
        assert local['visual_effect_global_changed'] is False, local
        report['cases']['qqmusic_button_only_reconstruction'] = local
        unchanged = dispatcher._compare_visual_effect(before, before, regions)
        assert unchanged['visual_effect'] is False, unchanged
        report['cases']['qqmusic_identical_frame'] = unchanged

print(json.dumps(report, ensure_ascii=False, indent=2))
