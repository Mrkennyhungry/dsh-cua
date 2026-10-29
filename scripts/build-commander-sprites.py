from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
source=Image.open(ROOT/'docs/images/whale-commander-sheet.png').convert('RGBA')
out=ROOT/'kernel/dsh_desktop/assets/whale_commander'
out.mkdir(parents=True,exist_ok=True)
w,h=source.size
for index in range(12):
    col,row=index%4,index//4
    cell=source.crop((round(col*w/4),round(row*h/3),round((col+1)*w/4),round((row+1)*h/3)))
    # Remove neighbouring effects only at the affected edges; keep feet/tails.
    inset=round(min(cell.size)*.045)
    if index in (6,7):cell=cell.crop((0,inset,cell.width,cell.height))
    if index == 9:cell=cell.crop((inset,0,cell.width,cell.height))
    cell=cell.resize((112,112),Image.Resampling.NEAREST)
    cell.save(out/f'{index:02d}.png')
print(out)
