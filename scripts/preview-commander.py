from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'kernel'))
from PIL import Image
from dsh_desktop.mascot_theme import render,WIDTH,HEIGHT
actions=('enter','thinking','click','right_click','type','scroll','drag','observe','done')
sheet=Image.new('RGBA',(WIDTH*3,HEIGHT*3),(228,237,249,255))
for index,action in enumerate(actions):
    sprite=Image.fromarray(render(action,0)[:,:,[2,1,0,3]],'RGBA')
    sheet.alpha_composite(sprite,((index%3)*WIDTH,(index//3)*HEIGHT))
out=ROOT/'docs/images/commander-preview.png'
sheet.save(out)
print(out)
