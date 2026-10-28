# DSH Desktop Automation

Windows desktop and browser automation for DSH. The Python kernel source ships inside this plugin under `kernel/dsh_desktop`; runtime setup installs only Python dependencies and local OCR/vision weights. The kernel is maintained and released with the plugin.

## Install the plugin

DSH installs plugins from a pnpm spec, so this repository can be installed directly — no npm publish step is required:

```bash
# Pin a release (recommended)
pnpm add "dsh-doraemon@git+https://github.com/Mrkennyhungry/dsh-cua.git#v0.4.5"
```

Add that line to the profile's `package.json` dependencies (or paste the same spec into the DSH plugin install dialog), then restart DSH. The spec also accepts a commit SHA instead of a tag, which is what a market index pins.

Requirements: Node.js >= 22, DSH >= 0.2.1-rc.2 < 0.3.0, Windows.

The 92 MB of ONNX weights are committed to this repository, so a fresh clone runs offline with no download step. GitHub warns above 50 MB per file; the largest file is 76.7 MB, under the 100 MB hard limit. Git LFS is deliberately not used so that installing never requires a second tool.

## Data boundary and what "success" means

Read [`docs/data-boundary.md`](docs/data-boundary.md) before enabling this on a machine with private content on screen. In short:

- OCR (RapidOCR) and icon detection (YOLO) run **locally**.
- Icon **captions** are the one place screen pixels leave the machine: when `DEEPSEEK_API_KEY` is set, `desktop_analyze(caption=true)` posts an annotated frame plus icon crops to `api.deepseek.com`. Without the variable it makes no network call.
- Tool results mix three strengths of evidence — **action sent**, **UI changed**, **business result verified**. `effect: "confirmed"` only means the screen moved, not that the task worked.

## Install the runtime

Run `scripts/bootstrap.ps1` from PowerShell. It creates a Python 3.11 virtual environment under `%LOCALAPPDATA%\dsh-desktop`, installs `requirements.txt`, and copies the bundled weights.

For applications launched with administrator privileges, run `scripts/install-elevated-task.ps1` once from an elevated PowerShell. The task starts the bundled kernel source with High integrity. Applications started directly by an elevated kernel can inherit its privileges. The plugin starts the kernel on first use and keeps it available between DSH sessions.

## Task window lifecycle

The first desktop/browser request records a lightweight window and browser-page baseline. End-of-task cleanup closes only task-owned launches and pages, preserves requested keep items, and reports remaining window differences. Existing desktop windows are never closed just because they were absent from the task's actions.

## Package variants

This generic build contains no company-specific page or component catalog.

Application launch confirms a new window and its process evidence. A forwarded launch or shared process that cannot be attributed returns `LAUNCH_UNCONFIRMED`; inspect the windows before retrying.

Elevated scheduled-task output is appended to `kernel.out.log` and `kernel.err.log` in the configured `stateDir` (or the task home `state` folder for older configurations). Cleanup sends a normal close request, reports an open save prompt as a failure, and does not force-terminate process-only records.

New tool-entry logs redact common text, path and credential fields, including values inside action batches. Existing log files are retained.
