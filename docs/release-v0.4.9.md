# dsh-doraemon v0.4.9

Recall fixes for `desktop_watch since`, all three found by reviewing the 0.4.8
code against its own stated rule: **小面积变化优先** (small changed areas are the
ones that carry messages; big ones are page churn).

## What changed

### 1. `limit` no longer decides which frames get a chance to be OCR'd

`since()` truncated the frame list with `selected[-limit:]` **before** the
budget logic ran. With the default `limit=8` every frame older than the newest
eight disappeared — including a small toast that appeared early in the window —
and none of `_select_targets`' small-area-first + even-sampling rules ever saw
it. The same "存在预算 → 必须选目标 → 选错就漏" shape the 0.4.8 budget work was
supposed to remove, just moved one step earlier.

`limit` is now only a **display** bound: frames are chosen with
`_select_frames()` (the same geometry as `_select_targets`), OCR cost is still
capped by `ocr_limit`, and the response carries `frames_in_window` plus a
`frames_warning` when frames were dropped, so a caller can tell that it may be
looking at a subset.

### 2. The `ROI_MAX_PER_FRAME` safety valve keeps the *smallest* rectangles

`_diff_rois()` sorted changed rectangles by area **descending** and kept the top
six, while its own docstring promised "nothing is ranked or selected". When one
frame produced more than six rectangles (page churn), the largest ones took
every slot and the toast — the smallest rectangle — was dropped. Sorting is now
ascending, matching `_select_targets`.

### 3. `ocr_limit=0` really means "no OCR"

`max(1, min(ocr_limit, MAX_OCR_BANDS))` silently turned an explicit 0 into one
OCR pass, in both the module and the tool. 0 is now honoured from the tool
through to the kernel: the call returns timeline, contact sheet and evidence
image with `ocr.runs == 0` and an explicit `ocr_limit=0` warning, and
`ocr.truncated` stays `false` so the model is not nudged towards "raise
ocr_limit".

## Install

```
dsh-doraemon@git+https://github.com/Mrkennyhungry/dsh-cua.git#v0.4.9
```

Or point **Plugins → Discover** at
`https://raw.githubusercontent.com/Mrkennyhungry/dsh-cua/main/market/index.json`.

## Verification

- `PYTHONPATH=kernel python -m pytest test -q` → 255 passed, 1 skipped, 13 subtests.
- Three new regressions in `test/test_watch_frame_limit.py`, one in
  `test/test_watch_diff_rois.py`.
- `node --test test/*.test.js` → 14/15. `test/plugin.test.js` needs the
  `@deepseek-ai/schemastery` peer dependency, which a bare clone does not have;
  it fails identically before these changes.

## Note on 0.4.8

The `v0.4.8` tag points at `4652b16` (release commit), but the 0.4.8 artifact
distributed through the market index was built from a later `main`, so the same
version string covered two different trees. 0.4.9 is tagged at the commit that
actually ships.
