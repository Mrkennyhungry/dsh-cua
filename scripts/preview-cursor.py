"""Render all bundled cursor themes and animation states without starting Windows UI."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'kernel'))
from dsh_desktop.cursor_theme import ACTIONS, PALETTES, render
from PIL import Image, ImageDraw
out=Path(sys.argv[1] if len(sys.argv)>1 else 'docs/images/cursor-themes.png')
out.parent.mkdir(parents=True,exist_ok=True)
w,h=120,140
sheet=Image.new('RGB',(w*len(ACTIONS),h*len(PALETTES)), '#e8edf4')
d=ImageDraw.Draw(sheet)
for row,palette in enumerate(PALETTES):
    for col,action in enumerate(ACTIONS):
        a=render(action=action,frame=1,palette=palette,scale=2)
        im=Image.fromarray(a[:,:,[2,1,0,3]],'RGBA')
        sheet.paste(im,(col*w+12,row*h+8),im)
        d.text((col*w+8,row*h+110),action,fill='#17283e')
        d.text((col*w+8,row*h+125),palette,fill='#405470')
sheet.save(out)
print(out.resolve())
