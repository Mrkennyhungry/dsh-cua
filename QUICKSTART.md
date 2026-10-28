# Quick start

One screen: what must be true, four steps, and how to tell it worked. Full reference is
[`README.md`](README.md).

## Before you start

| Requirement | Why, and what happens without it |
|---|---|
| **Windows 10/11** | The bundle declares `os: ["win32"]`; the package manager refuses to install it elsewhere. |
| **DSH 0.2.x** (`>=0.2.1-rc.2`, `<0.3.0` — the current stable line) | DSH enforces the range this package declares, so a DSH 0.3 would refuse to install it. **DSH 0.3 does not exist yet**; when it ships this plugin has to be retested and that upper bound will move. Nothing to do today. |
| **`uv` on `PATH`, and network access once** | Step 3 builds a Python 3.11 environment and downloads a few hundred MB of packages. |

Node.js >= 22 is also required (DSH already needs it).

## Four steps

**1. Install the bundle.** In DSH: **Plugins → Add plugin**, paste, confirm:

```
dsh-doraemon@git+https://github.com/Mrkennyhungry/dsh-cua.git#v0.4.5
```

Or switch on **Custom plugin markets**, add the index
`https://raw.githubusercontent.com/Mrkennyhungry/dsh-cua/main/market/index.json`
on **Plugins → Discover**, and install from there.

**2. Restart DSH.** The bundle is mounted at boot.

**3. Run bootstrap once**, from the installed package directory (or from a clone of the
repository — the script only needs `scripts/`, `kernel/`, `vendor/weights/` and
`requirements.txt`):

```powershell
cd "$env:USERPROFILE\.dsh\profiles\<your-profile>\node_modules\dsh-doraemon"
.\scripts\bootstrap.ps1
```

It creates `%LOCALAPPDATA%\dsh-desktop\venv`, installs `requirements.txt`, copies the
bundled weights, and imports the kernel to prove it works. 5–15 minutes, mostly
download.

**4. Restart DSH again**, then ask your agent:

> 用 doraemon 打开记事本，写一行 hello，然后把窗口标题读回来

Expect: Notepad opens, text appears, and the tool result reports what it verified.

## Do not skip step 3

Installing the plugin does **not** install the Python runtime. Until bootstrap has run
once, every desktop tool fails with:

```
Run the bundled scripts/bootstrap.ps1 or set repoRoot/pythonPath
```

That is the only error most people hit; it means step 3 was skipped or ran in the wrong
directory.

## Optional

- **Elevated windows** (apps running as administrator) need `scripts\install-elevated-task.ps1`
  once, from an elevated PowerShell. Without it, clicks into those windows are blocked by
  Windows UIPI.
- **Icon captions** call `api.deepseek.com` only when `DEEPSEEK_API_KEY` is set. Without
  it, OCR and icon detection run entirely locally and no screen pixels leave the machine.

## Read before pointing it at private content

[`docs/data-boundary.md`](docs/data-boundary.md) — what leaves the machine, and what
"success" means in a tool result. Short version: `effect: "confirmed"` means the screen
changed, not that the task worked.
