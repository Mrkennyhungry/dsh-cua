# v0.4.5 — Windows desktop automation for DSH

First public release of `dsh-doraemon`: a DSH bundle that lets an agent drive Windows
applications and the browser — window binding, UIA element trees, local OCR/YOLO
parsing, clicks, typing, clipboard, file drops — through a Python kernel that ships
inside the plugin.

## Install

Pin the release (a tag can move; the commit cannot):

```
dsh-doraemon@git+https://github.com/Mrkennyhungry/dsh-cua.git#1e234ec1fdda70d9a70ed1191d7ac327d28976f2
```

Or, with custom plugin markets switched on, add the index source
`https://raw.githubusercontent.com/Mrkennyhungry/dsh-cua/main/market/index.json`
on **Plugins → Discover** and install from there.

Then restart DSH. **Installing does not install the Python runtime** — run
`scripts/bootstrap.ps1` once (it needs [`uv`](https://docs.astral.sh/uv/) and network
for `requirements.txt`), then restart again.

## What is in the bundle

- **Kernel** — `kernel/dsh_desktop`, a Python service the plugin starts hidden on first
  use and keeps warm between sessions. `scripts/bootstrap.ps1` builds its virtual
  environment under `%LOCALAPPDATA%\dsh-desktop`.
- **Weights** — ~92 MB of OCR (RapidOCR) and icon-detection (YOLO) ONNX models are
  committed under `vendor/weights`, so no download step and no GPU service. Largest
  single file is 76.7 MB, under GitHub's 100 MB limit; Git LFS is deliberately unused.
- **Tools** — analyze/capture, click, type, keys, scroll, drag, clipboard, window
  management, plus task-level `doraemon_run` with status/steer/stop.

## Requirements

- Windows (the bundle declares `os: win32`)
- Node.js >= 22, DSH >= 0.2.1-rc.2 < 0.3.0
- `uv` on `PATH` for the first bootstrap

## Known limits

- OCR and YOLO run locally; **icon captions** are the one path where screen pixels
  leave the machine, and only when `DEEPSEEK_API_KEY` is set. See
  [`docs/data-boundary.md`](https://github.com/Mrkennyhungry/dsh-cua/blob/main/docs/data-boundary.md).
- A tool result saying `effect: "confirmed"` means the screen changed, not that the
  business result was verified — check the verification field.
- The package is large by design (weights in-tree); `pnpm add` from git takes ~15 s on
  a warm connection.

## License

MIT, with third-party weight licenses recorded in `THIRD_PARTY_NOTICES.txt`.
