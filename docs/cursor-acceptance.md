# Cursor implementation checks

> Historical virtual-cursor acceptance. The current physical-input commander
> design and its live results are in commander-mode.md and commander-acceptance.json.

2026-10-29. Changes are in the complete `default-workspace/plugins/dsh-doraemon`
checkout, linked by the installed DSH profile. No commit or publication was made.

Implemented:

- The user's reference-based imagegen sheet is the default `whale-maid` renderer.
  Twelve 48px action frames and four thinking frames ship inside `kernel/`.
- Three palettes; scale 1 is 48px, scale 2 is 96px. The prior code-drawn design is
  available as `classic`; local Python style plugins remain supported.
- Host step/turn lifecycle plus internal Python task lifetime activate thinking.
  Tools override thinking with their concrete action; completion clears ownership.
- Screenshot acquisition temporarily hides the sprite; alpha is premultiplied for
  Windows layered-window rendering. Breathing period and idle retention are 2s.
- Default virtual mouse input does not silently fall back to physical input.
  Apps can ignore posted messages; these results remain effect-unconfirmed.

Checks completed against the final source:

| Check | Result |
| --- | --- |
| Theme/asset and cursor lifecycle Python tests | 8 passed |
| Virtual input routing Python tests | 11 passed |
| Virtual pointer coordinate reporting | 2 passed |
| DSH lifecycle routing Node tests | 3 passed |
| Python compileall | passed |
| index.js and cursor-state.js syntax | passed |
| git diff --check | passed (line-ending notices only) |
| Preview | docs/images/cursor-themes-v2.png generated and visually inspected |
| Package paths | kernel/**/* covers PNGs; cursor-state.js and reload helper listed |

Package inclusion was checked against the explicit file list; `npm pack --dry-run`
was unavailable because npm is not on this shell's PATH.

## Direct verification after the reported bug

`scripts/verify-native-cursor.py` creates its own native Windows parent, child
canvas and Edit control. It directly imports InputService without any agent or
DSH client. Thirteen checks passed, including exact target coordinates, button
events, scroll-state changes, dragging an object, Chinese text readback,
backspace, select-all replacement, visible sprite movement, thinking and hiding.

`scripts/verify-native-cursor.py --bridge` additionally calls the actual running
authenticated HTTP kernel. No model request or DSH client is used. Twenty-two
checks (including the native baseline) passed. The test reads back the Edit
control, verifies the native child's received events and scroll/drag state,
checks cursor targets and verifies the loaded virtual input revision. See
`cursor-native-results.json` and `cursor-http-results.json` for exact observations.
The physical mouse is sampled throughout; no change was observed.

Fixes include retaining the bound HWND down to the native child, sharing the
overlay without importing the agent registry, forwarding virtual keyboard
input to native Edit controls, and reporting the virtual pointer rather than
the physical cursor in observations. Known-incompatible Tk message clicks now
return an unsupported error, instead of claiming a successful posted click.
The running kernel was restarted and its imported input revision verified
against the on-disk input.py SHA256 prefix. Its health now exposes input mode,
source path, loaded revision, and virtual cursor position/error.

The earlier delegated Tk checks were not a pass. Cross-application acceptance
still requires each application's semantic DOM/UIA route or a compatible native
message target. Host event propagation and unusual DPI/multiple-monitor layouts
are not covered by the native test.
Chromium/Electron coordinate input and cross-window/OLE drags may require DOM/UIA
or explicit physical mode. Virtual text/key messages support native Edit/RichEdit
controls; custom inputs should use DOM/UIA. Physical mode retains focus checks.

The earlier full JS suite could not run because its dependencies and an existing
tool-registry.js reference were unavailable; focused lifecycle tests run independently.

See [cursor-themes.md](cursor-themes.md) for reloading and configuration.
