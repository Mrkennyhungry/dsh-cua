# dsh-doraemon v0.4.5

Windows desktop automation for DSH: an agent drives Windows applications and the
browser — window binding, UIA element trees, local OCR/YOLO parsing, clicks, typing,
clipboard, file drops — through a Python kernel that ships inside the plugin.

## Install (about 15 minutes, most of it downloads)

> Windows and DSH **0.2.x** only. Installing the bundle does **not** install the Python
> runtime; step 2 does.

**1. Install the bundle.** DSH → **Plugins → Add plugin**, paste, confirm:

```
dsh-doraemon@git+https://github.com/Mrkennyhungry/dsh-cua.git#v0.4.5
```

Then restart DSH.

**2. Run bootstrap once.** Needs [`uv`](https://docs.astral.sh/uv/) on `PATH`. From the
installed package directory:

```powershell
cd "$env:USERPROFILE\.dsh\profiles\<your-profile>\node_modules\dsh-doraemon"
.\scripts\bootstrap.ps1
```

It builds `%LOCALAPPDATA%\dsh-desktop\venv`, installs `requirements.txt`, copies the
bundled weights, and imports the kernel to prove it works. 5–15 minutes.

**3. Restart DSH and try it.** Ask your agent:

> 用 doraemon 打开记事本，写一行 hello，然后把窗口标题读回来

Skipping step 2 is the one error almost everyone hits:

```
Run the bundled scripts/bootstrap.ps1 or set repoRoot/pythonPath
```

Prefer the market? Switch on **Custom plugin markets**, add the index
`https://raw.githubusercontent.com/Mrkennyhungry/dsh-cua/main/market/index.json` on
**Plugins → Discover**, and install from there. One screen of the same steps lives in
[`QUICKSTART.md`](https://github.com/Mrkennyhungry/dsh-cua/blob/main/QUICKSTART.md).

## What is in the bundle

- **Kernel** — `kernel/dsh_desktop`, a Python service the plugin starts hidden on first
  use and keeps warm between sessions.
- **Weights** — ~92 MB of OCR (RapidOCR) and icon-detection (YOLO) ONNX models are
  committed under `vendor/weights`, so no download step and no GPU service. Largest
  single file is 76.7 MB, under GitHub's 100 MB limit; Git LFS is deliberately unused,
  so installing never needs a second tool.
- **Tools** — analyze/capture, click, type, keys, scroll, drag, clipboard, window
  management, plus task-level `doraemon_run` with status/steer/stop.

## Requirements

- Windows (the bundle declares `os: win32`)
- Node.js >= 22
- DSH 0.2.x — declared as `>=0.2.1-rc.2 <0.3.0` in `engines.dsh`, and DSH enforces it.
  DSH 0.3 does not exist yet; when it ships this plugin needs retesting before that
  upper bound moves.
- `uv` on `PATH` for the first bootstrap

## Known limits

- OCR and YOLO run locally; **icon captions** are the one path where screen pixels
  leave the machine, and only when `DEEPSEEK_API_KEY` is set. See
  [`docs/data-boundary.md`](https://github.com/Mrkennyhungry/dsh-cua/blob/main/docs/data-boundary.md).
- A tool result saying `effect: "confirmed"` means the screen changed, not that the
  business result was verified — check the verification field.
- Automating windows that run as administrator needs
  `scripts\install-elevated-task.ps1` once, from an elevated PowerShell.

## License

MIT, with third-party weight licenses recorded in `THIRD_PARTY_NOTICES.txt`.
