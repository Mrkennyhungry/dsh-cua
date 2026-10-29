"""Tiny pixel whale-maid theme plugins (straight-alpha BGRA)."""
import importlib.util
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
ACTIONS = ('idle','thinking','move','click','type','key','scroll','drag','observe','error')
PALETTES = {'ocean': ('#469bd1','#a6e5ef','#20395f'), 'lavender': ('#9484ce','#d5c7f4','#44375e'), 'mint': ('#4caeab','#b4efcf','#244c57')}
# Canvas size per bundled theme; custom plugins keep whatever size they return.
CANVAS_SIZE = {'whale-maid': 48, 'classic': 36}
ASSET_DIR = Path(__file__).with_name('assets') / 'cursor_whale_maid'
FRAME_SIZE = 48
# Sprite sheet frame for each action: reading order in the 4x3 reference sheet.
FRAME_MAP = {'idle': (0, 1), 'move': (2, 3), 'click': (4, 5), 'type': (6, 7),
             'key': (6, 7), 'scroll': (8,), 'drag': (9,), 'observe': (10,), 'error': (11,)}
_RENDERERS = {}
_PLUGINS_LOADED = False

def register_theme(name, renderer):
    if not name or not callable(renderer): raise ValueError('theme name and callable required')
    _RENDERERS[name] = renderer

def themes():
    return tuple(_RENDERERS)

def load_theme_file(path):
    """Load one local Python theme file; it must define render(action, frame, palette)."""
    path = Path(path).expanduser().resolve()
    if path.suffix != '.py' or not path.is_file(): raise ValueError(f'not a theme file: {path}')
    spec = importlib.util.spec_from_file_location(f'dsh_cursor_theme_{path.stem}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    renderer = getattr(module, 'render', None)
    if not callable(renderer): raise ValueError(f'{path.name} must define render(action, frame, palette)')
    register_theme(path.stem, renderer)
    return path.stem

def load_theme_dir(directory=None):
    """Load every *.py theme in an explicit local directory (no downloads, no eval of remote code)."""
    directory = Path(directory or os.environ.get('DSH_CURSOR_THEME_DIR')
                     or Path.home() / '.dsh-doraemon' / 'cursor-themes').expanduser()
    if not directory.is_dir(): return ()
    loaded = []
    for path in sorted(directory.glob('*.py')):
        if path.name.startswith('_'): continue
        try: loaded.append(load_theme_file(path))
        except Exception: continue   # a broken style must never break the cursor
    return tuple(loaded)

def _load_plugins_once():
    global _PLUGINS_LOADED
    if not _PLUGINS_LOADED:
        _PLUGINS_LOADED = True
        try: load_theme_dir()
        except Exception: pass

def render(theme='whale-maid', action='idle', frame=0, palette='ocean', scale=1):
    _load_plugins_once()
    if theme not in _RENDERERS or action not in ACTIONS or palette not in PALETTES: raise ValueError('unknown style/action/palette')
    if scale not in (1,2): raise ValueError('scale must be 1 or 2')
    a = np.asarray(_RENDERERS[theme](action,frame,palette))
    if a.dtype != np.uint8 or a.ndim != 3 or a.shape[2] != 4: raise ValueError('theme must return uint8 BGRA HxWx4')
    return np.repeat(np.repeat(a,scale,0),scale,1)

def assets_ready():
    """True when the bundled 4x3 sheet was sliced into all 12 frames."""
    try: manifest = __import__('json').loads((ASSET_DIR / 'manifest.json').read_text(encoding='utf-8'))
    except Exception: return False
    return len(manifest.get('frames') or ()) == 12 and all(
        (ASSET_DIR / f['file']).is_file() for f in manifest['frames'])

@lru_cache(maxsize=None)
def _sprite(index):
    """Load one 48px frame as BGRA; the sheet ships pre-sliced, no runtime download."""
    from PIL import Image
    path = ASSET_DIR / f'frame_{index:02d}.png'
    image = Image.open(path).convert('RGBA')
    if image.size != (FRAME_SIZE, FRAME_SIZE):
        image = image.resize((FRAME_SIZE, FRAME_SIZE), Image.NEAREST)
    return np.ascontiguousarray(np.asarray(image)[:, :, [2, 1, 0, 3]], dtype=np.uint8)

def _whale(action, frame, palette):
    """Default theme: the sliced reference sprite sheet, anchored at its top-left pixel."""
    try:
        index = FRAME_MAP[action][frame % len(FRAME_MAP[action])]
        sprite = _sprite(index).astype(np.float32)
    except Exception:
        return _classic(action, frame, palette)   # missing/corrupt asset never hides the cursor
    if palette != 'ocean':
        tint = np.array([int(PALETTES[palette][0][i:i+2], 16) for i in (1, 3, 5)], np.float32) / 255.0
        strength = 0.30
        sprite[:, :, :3] = sprite[:, :, :3] * ((1.0 - strength) + strength * tint)
    return np.clip(sprite, 0, 255).astype(np.uint8)

def _classic(action,frame,palette):
    c=np.zeros((36,36,4),dtype=np.uint8)
    def color(h): return tuple(int(h[i:i+2],16) for i in (5,3,1))+(255,)
    hair,light,dress=map(color,PALETTES[palette])
    outline,white,skin,blush=map(color,('#17283e','#f1f6ff','#ffdbca','#ed9eae'))
    bob=frame%2 if action in ('idle','move','observe') else 0
    def box(x,y,w,h,col):
        y+=bob
        c[max(y,0):min(y+h,36),max(x,0):min(x+w,36)]=col
    # Fixed hotspot at (0,0), character offset from target.
    box(0,-bob,3,1,white);box(0,-bob,1,3,white);box(1,1-bob,1,1,outline)
    box(24,22,4,6,outline);box(26,20,4,5,hair);box(27,18,6,3,hair);box(30,16,3,3,light)
    box(4,10,5,4,outline);box(4,10,4,2,light);box(23,10,5,4,outline);box(24,10,4,2,light)
    box(8,5,16,18,outline);box(9,6,14,17,hair);box(8,22,4,6,light);box(21,22,4,6,light)
    box(10,4,12,3,white)
    for x in (9,13,17,21):box(x,3,2,2,white)
    box(11,10,10,9,skin);box(10,8,12,4,hair);box(11,9,4,4,light);box(18,9,3,3,light)
    for x in (12,18):
        box(x,13,2,1 if action=='error' else 3,outline);box(x,13,1,1,white)
    box(11,16,2,1,blush);box(20,16,2,1,blush);box(16,17,2,1,outline)
    box(10,20,13,9,dress);box(8,26,17,3,dress);box(13,20,7,8,white)
    box(15,24,3,2,hair);box(18,23,2,2,hair);box(10,29,4,2,outline);box(20,29,4,2,outline)
    if action=='click':
        box(5,8 if frame%2 else 16,5,3,skin)
        if frame%2:box(2,3,1,3,light);box(4,2,3,1,light)
    elif action in ('type','key'):
        box(7,25,19,5,outline);box(8,26,17,3,light)
        for x in range(9,25,3):box(x,27,1,1,dress)
        box(10,23+frame%2,4,3,skin);box(20,24-frame%2,4,3,skin)
    elif action=='scroll':box(4,18,2,8,light);box(2,18 if frame%2 else 24,6,2,white)
    elif action=='drag':box(3,19,8,3,skin);box(1,17,3,6,light)
    elif action=='observe':box(22,12,7,7,outline);box(23,13,5,5,light);box(27,18,4,2,outline)
    elif action=='move':
        for x in (2,4,6):box(x,23+(x//2+frame)%3,2,1,light)
    elif action=='error':box(29,4,2,5,blush);box(29,10,2,2,blush)
    else:box(7,21,4,3,skin);box(22,21,4,3,skin)
    return c
register_theme('classic',_whale)

_SPRITES = {}
_FRAME_MAP = {'idle': (0, 1), 'move': (2, 3), 'click': (4, 5),
              'type': (6, 7), 'key': (6, 7), 'scroll': (8,),
              'drag': (9,), 'observe': (10,), 'error': (11,)}

def _sprite(action, frame, palette):
    import cv2
    if action == 'thinking':
        filename = f'thinking_{frame % 4:02d}.png'
    else:
        indices = _FRAME_MAP[action]
        filename = f'frame_{indices[frame % len(indices)]:02d}.png'
    if filename not in _SPRITES:
        path = Path(__file__).parent / 'assets' / 'cursor_whale_maid' / filename
        bitmap = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if bitmap is None or bitmap.shape != (48, 48, 4):
            raise ValueError(f'missing or invalid cursor asset: {path.name}')
        _SPRITES[filename] = bitmap
    bitmap = _SPRITES[filename].copy()
    if palette != 'ocean':
        hsv = cv2.cvtColor(bitmap[:, :, :3], cv2.COLOR_BGR2HSV)
        mask = (hsv[:, :, 0] >= 85) & (hsv[:, :, 0] <= 135) & (hsv[:, :, 1] > 60)
        hsv[:, :, 0][mask] = (hsv[:, :, 0][mask].astype(int) + (22 if palette == 'lavender' else -35)) % 180
        bitmap[:, :, :3] = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    # A stable one-pixel target marker; character stays offset from the hotspot.
    bitmap[0, 0] = (255, 255, 255, 255)
    return bitmap

register_theme('whale-maid', _sprite)
register_theme('classic',_classic)
