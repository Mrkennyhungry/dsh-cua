"""Measure local OCR and icon initialization independently with networking denied."""
import json
from pathlib import Path
import socket
import sys
import time
from unittest import mock

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from dsh_desktop.runtime import DesktopRuntime, RuntimeConfig


home = Path(sys.argv[1]).resolve()
home.mkdir(parents=True, exist_ok=True)
config = RuntimeConfig.from_home(home)
config.workspace.weights_dir = str(Path(sys.argv[2]).resolve())
runtime = DesktopRuntime(config, home=home, state_dir=home / 'state')
image = Image.new('RGB', (500, 180), 'white')
font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 22)
ImageDraw.Draw(image).text((30, 60), '中文小字 A7B9', font=font, fill='black')
pixels = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
report = {}
try:
    with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('Vision attempted a network connection')) as network:
        started = time.perf_counter()
        text = runtime.perceive(pixels, 'ocr')
        report['ocr_ms'] = round((time.perf_counter() - started) * 1000, 3)
        joined = ' '.join(node['text'] for node in text)
        assert '中文小字' in joined and 'A7B9' in joined, joined
        assert runtime._vision_parsers['ocr'].yolo_session is None
        report['ocr_text'] = joined
        report['ocr_initialized_yolo'] = 0
        started = time.perf_counter()
        icons = runtime.perceive(pixels, 'yolo')
        report['yolo_ms'] = round((time.perf_counter() - started) * 1000, 3)
        assert runtime._vision_parsers['yolo'].ocr is None
        report['yolo_initialized_ocr'] = 0
        report['yolo_detections'] = len(icons)
        report['network_connections'] = network.call_count
        report['caption_requests'] = 0
        report['parser_initializations'] = runtime.parser_initializations
finally:
    runtime.shutdown()
print(json.dumps(report, ensure_ascii=False))
