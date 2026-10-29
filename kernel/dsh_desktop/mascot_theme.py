"""Fixed-position commander art and speech bubbles; never an input cursor."""
from functools import lru_cache
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 300, 204
SPRITE_SIZE = 112
ACTIONS = ('enter','thinking','idle','move','click','right_click','type','key','scroll','drag','observe','done','error')
FRAMES = {'enter':(0,1),'thinking':(2,3),'idle':(2,3),'move':(4,5),
          'click':(4,5),'right_click':(4,5),'type':(6,7),'key':(6,7),
          'scroll':(8,),'drag':(9,),'observe':(10,),'done':(11,),'error':(2,3)}
CAPTIONS = {'enter':'终于轮到我玩电脑了！','thinking':'让我想想下一步……',
            'idle':'我在这里，随时待命！','move':'让鼠标飞过去～','click':'挥挥魔杖，点这里！',
            'right_click':'右键菜单，听我号令！','type':'文字魔法，正在输入～',
            'key':'按键咒语，准备好了！','scroll':'卷轴翻页术！','drag':'抓住它，慢慢移动～',
            'observe':'让我仔细瞧瞧……','done':'任务完成，收工啦！','error':'等一下，我再看看……'}

@lru_cache(maxsize=12)
def sprite(index):
    return Image.open(Path(__file__).parent/'assets'/'whale_commander'/f'{index:02d}.png').convert('RGBA')

@lru_cache(maxsize=1)
def font():
    for path in ('C:/Windows/Fonts/msyh.ttc','C:/Windows/Fonts/simhei.ttf'):
        if Path(path).exists(): return ImageFont.truetype(path,17)
    return ImageFont.load_default()

@lru_cache(maxsize=64)
def _render(action, frame, caption):
    canvas = Image.new('RGBA',(WIDTH,HEIGHT))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((5,5,289,67),radius=10,fill=(248,251,255,245),outline=(74,100,153,255),width=2)
    draw.polygon(((218,66),(234,81),(243,65)),fill=(248,251,255,245))
    draw.line(((218,67),(234,81),(243,67)),fill=(74,100,153,255),width=2)
    text = caption or CAPTIONS[action]
    for line in range(0,len(text),15):
        draw.text((16,16+line//15*24),text[line:line+15],font=font(),fill=(40,58,93,255))
    indices=FRAMES[action]
    image=sprite(indices[frame % len(indices)])
    canvas.alpha_composite(image,(WIDTH-SPRITE_SIZE-10,HEIGHT-SPRITE_SIZE-7))
    return np.asarray(canvas)[:,:,[2,1,0,3]].copy()

def render(action='thinking',frame=0,caption=None):
    return _render(action if action in ACTIONS else 'thinking',frame % 4,caption).copy()
