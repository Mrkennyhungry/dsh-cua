# dsh-doraemon v0.4.6

Observation payload and click-coordinate release. The kernel is unchanged in what it
can do; what changed is what it says, and how much of it.

**Upgrading from 0.4.5 matters.** Two defects in 0.4.5 made the agent misread the
screen: click points were published under a name (`x`/`y`) that looks like screenshot
pixels, so a normalized `y=666` on a 1226–800 window was scaled a second time and sent
as `833` — a click on a different row. Element fields are also renamed (see below), so
anything that parsed the old payload needs updating.

## Install

```
dsh-doraemon@git+https://github.com/Mrkennyhungry/dsh-cua.git#v0.4.6
```

Or add the index `https://raw.githubusercontent.com/Mrkennyhungry/dsh-cua/main/market/index.json`
on **Plugins → Discover** — it now defaults to 0.4.6. Steps 2–4 of
[`QUICKSTART.md`](https://github.com/Mrkennyhungry/dsh-cua/blob/main/QUICKSTART.md)
(bootstrap the Python runtime, restart, verify) are unchanged.

## Click coordinates

- **`click_x` / `click_y`, never bare `x`/`y`.** The point is final: pass it straight to
  `doraemon_click`. Do not divide by the image size.
- **`coordinate_contract`** states the unit once per document — `normalized_0_1000` for a
  window-scope capture, `screen_pixels` for `scope="desktop"` — plus `window_px` and
  `observation_px` for orientation. It deliberately publishes **no** pixels-to-normalized
  formula: that conversion lives in `doraemon_click_image_point`, and putting the formula
  in the payload invited a second scaling.
- **`click_space`** says which tool the point belongs to: `window` → `doraemon_click`,
  `screen` → `doraemon_click_screen`.
- **`hit_check`** on every click: the element under the point, or `CLICK_HIT_EMPTY` with
  the nearest element's `click_x`/`click_y` and distance. A click 360 px off target used
  to return plain success.

## Occlusion

A window-scope capture is a screen crop, so anything lying on top of the target is in the
picture — and its text is in the element list. `occlusion` now names those windows
(`title`, `class`, `overlap`) and points at `scope="desktop"` when the whole layer is what
you meant to look at.

## Smaller observations

| Observation | Before | 0.4.6 |
|---|---|---|
| UIA, 236-control Explorer window | 21.2 KB (full fields) | 9.8 KB per 120 controls, whole window in ~20 KB |
| UIA filters | — | `control_types`, `text_contains`, `only_actionable` |
| UIA node budget | 120 (cut a 230-control window in half) | 300, ~60–80 B per control |
| analyze, 150 elements | 19.8 KB | 10.9 KB; `only_actionable` trims 150 → 90 |

Filters trim the **element list only** — never the screenshot: `only_actionable` drops
elements whose text and caption are both placeholders (`icon`, `未知`), which cannot be
clicked by name anyway.

## Breaking changes for existing scripts

- `ui_elements[].x` / `.y` → `click_x` / `click_y` (analyze).
- `controls[].x` / `.y` → `click_x` / `click_y` (compact `doraemon_uia`).
- `ui_elements[].bbox` and `.id` are no longer published by the HTTP surface (internal
  callers still see them).
- `hit_check.nearest.x` / `.y` → `click_x` / `click_y`.

## Requirements

Windows, Node.js >= 22, DSH 0.2.x (`>=0.2.1-rc.2 <0.3.0`, enforced). DSH 0.3 does not
exist yet; this plugin needs retesting before that upper bound moves.
