"""Slice the 4x3 whale-maid reference sheet into 48px cursor frames.

Source: docs/images/whale-maid-reference-v2.png (1448x1086 RGBA, 362x362 cells).
Output: kernel/dsh_desktop/assets/cursor_whale_maid/ so the packaged kernel can
draw the sprite theme without any runtime download.

Frame order is reading order (left to right, top to bottom); cursor_theme.py maps
actions to those frame indices. Re-run after replacing the reference sheet:

    .runtime/venv/Scripts/python.exe scripts/build-cursor-sprites.py
"""
import json
import shutil
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'docs' / 'images' / 'whale-maid-reference-v2.png'
OUT = ROOT / 'kernel' / 'dsh_desktop' / 'assets' / 'cursor_whale_maid'
COLUMNS, ROWS = 4, 3
FRAME_SIZE = 48


def main() -> int:
    if not SOURCE.is_file():
        raise SystemExit(f'missing reference sheet: {SOURCE}')
    OUT.mkdir(parents=True, exist_ok=True)
    sheet = Image.open(SOURCE).convert('RGBA')
    width, height = sheet.size
    cell_w, cell_h = width // COLUMNS, height // ROWS
    if (cell_w, cell_h) != (width / COLUMNS, height / ROWS):
        raise SystemExit(f'sheet {width}x{height} is not evenly divisible into {COLUMNS}x{ROWS}')
    # Keep the original sheet next to the frames: the kernel can re-slice it if a
    # single frame file is ever missing, and the asset stays self-describing.
    shutil.copyfile(SOURCE, OUT / 'sheet.png')
    frames = []
    for index in range(COLUMNS * ROWS):
        column, row = index % COLUMNS, index // COLUMNS
        cell = sheet.crop((column * cell_w, row * cell_h,
                           (column + 1) * cell_w, (row + 1) * cell_h))
        frame = cell.resize((FRAME_SIZE, FRAME_SIZE), Image.NEAREST)
        name = f'frame_{index:02d}.png'
        frame.save(OUT / name, optimize=True)
        frames.append({'index': index, 'row': row, 'column': column, 'file': name,
                       'size': [FRAME_SIZE, FRAME_SIZE], 'source_cell': [cell_w, cell_h]})
    (OUT / 'manifest.json').write_text(json.dumps(
        {'source': 'docs/images/whale-maid-reference-v2.png',
         'grid': {'columns': COLUMNS, 'rows': ROWS, 'cell': [cell_w, cell_h]},
         'frame_size': FRAME_SIZE, 'resample': 'nearest',
         'frames': frames}, indent=2) + '\n', encoding='utf-8')
    thinking = Image.open(ROOT / 'docs/images/whale-maid-thinking-v2.png').convert('RGBA')
    tw, th = thinking.size
    cells = [thinking.crop((round(i * tw / 4), 0, round((i + 1) * tw / 4), th)) for i in range(4)]
    bounds = [cell.getbbox() for cell in cells]
    common = (min(b[0] for b in bounds), min(b[1] for b in bounds),
              max(b[2] for b in bounds), max(b[3] for b in bounds))
    for index in range(4):
        cell = cells[index].crop(common)
        cell.thumbnail((FRAME_SIZE, FRAME_SIZE), Image.Resampling.NEAREST)
        frame = Image.new('RGBA', (FRAME_SIZE, FRAME_SIZE))
        frame.alpha_composite(cell, ((FRAME_SIZE - cell.width) // 2, FRAME_SIZE - cell.height))
        frame.save(OUT / f'thinking_{index:02d}.png', optimize=True)
    print(f'wrote {len(frames)} frames to {OUT}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
