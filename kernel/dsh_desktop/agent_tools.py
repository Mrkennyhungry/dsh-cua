"""Base agent capability tools — Python execution, shell commands, pip.

Registered under the "agent_base" toolset and enabled for every session
(see eternity.ENABLED_TOOLSETS). This closes the classic agent gap: the
model could read/write files but could not RUN anything, so tasks like
"read this docx" degenerated into opening Word + OCR. With run_python +
pip_install, the agent can parse documents, convert formats, compute, and
batch-edit files directly — GUI automation stays for what only the GUI
can do.

Security posture (owner-approved for VM/desktop-agent use):
- run_python executes arbitrary code with the configured interpreter —
  that is the product's purpose, same trust level as desktop_* desktop tools.
- run_command (arbitrary shell) is gated by tools.allow_shell (default
  OFF); when disabled the tool refuses with guidance instead of executing.
- Every execution is sandboxed in process terms only: hard timeout with
  whole-tree kill (taskkill /T), captured stdout/stderr with size caps,
  UTF-8 forced for predictable output, temp scripts cleaned up.
- python interpreter: config.tools.python_path (Settings UI) wins;
  fallback is Desktop's own sys.executable so pip installs land in an
  environment run_python can actually import.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .tool_decorator import tool
from .config import dsh_desktop_home

logger = logging.getLogger(__name__)

AGENT_TOOLSET = "agent_base"

# Output caps — the agent's context budget is the real constraint.
_MAX_OUTPUT_CHARS = 16_000
_DEFAULT_TIMEOUT = 60.0


class ConfigRef:
    """Live Config instance, injected by eternity.setup()."""

    current = None


def _tools_cfg():
    """Return the ToolsConfig section (or a permissive default when unset)."""
    from .config import ToolsConfig

    cfg = ConfigRef.current
    tools = getattr(cfg, "tools", None)
    return tools if tools is not None else ToolsConfig()


def _resolve_python() -> tuple[str, str]:
    """Resolve the interpreter for run_python/pip_install.

    Returns (executable, source) where source is "config" | "builtin".
    """
    configured = (_tools_cfg().python_path or "").strip().strip('"')
    if configured:
        # Accept a bare name on PATH ("python", "python3.11") or a full path.
        exe = shutil.which(configured)
        if exe:
            return exe, "config"
        p = Path(configured)
        if p.is_file():
            return str(p), "config"
        logger.warning("config.tools.python_path %r not found — falling back to builtin", configured)
    return sys.executable, "builtin"


def _decode_output(data: bytes) -> str:
    """Decode subprocess output: UTF-8 first, then the GBK console codepage."""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text, False
    return text[:_MAX_OUTPUT_CHARS], True


def _run_subprocess(cmd: list[str], timeout_s: float, cwd: str | None) -> dict:
    """Run a command, capture output, enforce timeout with a whole-tree kill."""
    t0 = time.time()
    env = os.environ.copy()
    # Predictable encoding for child Python processes; harmless elsewhere.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd or None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        return {"success": False, "error": f"executable not found: {exc.filename}"}
    except OSError as exc:
        return {"success": False, "error": f"failed to start: {exc}"}

    timed_out = False
    try:
        out_b, err_b = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Kill the whole process tree (children survive plain .kill() on Windows).
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        out_b, err_b = proc.communicate()

    out, out_trunc = _clip(_decode_output(out_b or b""))
    err, err_trunc = _clip(_decode_output(err_b or b""))
    elapsed_ms = round((time.time() - t0) * 1000)

    result: dict = {
        "success": not timed_out and proc.returncode == 0,
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "stdout": out,
        "stderr": err,
        "duration_ms": elapsed_ms,
    }
    if timed_out:
        result["timed_out"] = True
        result["error"] = f"timed out after {timeout_s:.0f}s (process tree killed)"
    if out_trunc or err_trunc:
        result["truncated"] = True
        result["note"] = "output truncated to 16000 chars per stream"
    return result


def _effective_timeout(requested: float) -> float:
    cap = float(_tools_cfg().max_timeout or 600)
    try:
        t = float(requested)
    except (TypeError, ValueError):
        t = _DEFAULT_TIMEOUT
    if t <= 0:
        t = _DEFAULT_TIMEOUT
    return min(t, cap)


def _workdir(workdir: str) -> tuple[str | None, str | None]:
    """Validate the requested working directory. Returns (cwd, error)."""
    if not workdir:
        return str(dsh_desktop_home()), None
    p = Path(workdir)
    if not p.is_dir():
        return None, f"workdir does not exist: {workdir}"
    return str(p), None


# ── Tools ────────────────────────────────────────────────────────────────


@tool("Execute a Python code snippet with the configured interpreter and return stdout/stderr/exit code. Use this for anything code-shaped: parse/convert documents (docx/xlsx/pdf via pip libs), process text, compute, batch-edit files. Non-interactive: print() your results. If an import fails, pip_install the package first.")
def run_python(code: str, timeout_seconds: float = 60, workdir: str = "") -> dict:
    """
    Args:
        code: Python source code to execute. print() to return output.
        timeout_seconds: Max seconds to wait (default 60, capped by settings).
        workdir: Working directory for the script (default: agent home).
    """
    code = (code or "").strip()
    if not code:
        return {"success": False, "error": "code 不能为空"}
    cwd, err = _workdir(workdir)
    if err:
        return {"success": False, "error": err}

    python_exe, source = _resolve_python()
    tmp_dir = dsh_desktop_home() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    script = tmp_dir / f"agent_py_{int(time.time())}_{uuid.uuid4().hex[:8]}.py"
    try:
        script.write_text(code, encoding="utf-8")
        result = _run_subprocess([python_exe, str(script)], _effective_timeout(timeout_seconds), cwd)
    finally:
        try:
            script.unlink(missing_ok=True)
        except OSError:
            pass
    result["python"] = python_exe
    result["python_source"] = source
    return result


@tool("Install Python packages with pip into the configured interpreter so run_python can import them. Example: packages=\"python-docx openpyxl\". Use extra_args for index mirrors, e.g. \"-i https://pypi.tuna.tsinghua.edu.cn/simple\".")
def pip_install(packages: str, timeout_seconds: float = 300, extra_args: str = "") -> dict:
    """
    Args:
        packages: Space-separated package names (pip syntax, no shell needed).
        timeout_seconds: Max seconds to wait (default 300, capped by settings).
        extra_args: Optional extra pip flags, e.g. a custom index URL.
    """
    pkgs = shlex.split(packages or "", posix=False)
    pkgs = [p.strip('"') for p in pkgs if p.strip()]
    if not pkgs:
        return {"success": False, "error": "packages 不能为空"}
    extra = shlex.split(extra_args or "", posix=False) if extra_args else []
    python_exe, source = _resolve_python()
    result = _run_subprocess(
        [python_exe, "-m", "pip", "install", "--disable-pip-version-check", *pkgs, *extra],
        _effective_timeout(timeout_seconds), None,
    )
    result["packages"] = pkgs
    result["python"] = python_exe
    result["python_source"] = source
    return result


@tool("Run a shell command (PowerShell or cmd) and return its output. REFUSED unless the user enabled allow_shell in Settings → 高级配置. When refused, fall back to run_python which is always available.")
def run_command(command: str, timeout_seconds: float = 60, shell: str = "powershell") -> dict:
    """
    Args:
        command: The shell command to execute.
        timeout_seconds: Max seconds to wait (default 60, capped by settings).
        shell: "powershell" (default) or "cmd".
    """
    if not (_tools_cfg().allow_shell):
        return {
            "success": False,
            "error": (
                "run_command 未启用：请让用户在 设置 → 高级配置 → 「允许 shell 命令」勾选后重试。"
                "大多数需求可用 run_python 完成（文件处理/解析/计算/批量操作）。"
            ),
        }
    command = (command or "").strip()
    if not command:
        return {"success": False, "error": "command 不能为空"}
    shell_key = (shell or "powershell").lower()
    if shell_key in ("powershell", "pwsh", "ps"):
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]
    elif shell_key == "cmd":
        cmd = ["cmd", "/c", command]
    else:
        return {"success": False, "error": f"不支持的 shell: {shell}（可选 powershell / cmd）"}
    return _run_subprocess(cmd, _effective_timeout(timeout_seconds), None)


@tool("Declare a multi-step autonomous task terminal only after verification. Call exactly once immediately before the final response, never for progress updates. status='completed' requires concrete evidence; for counted/bulk work provide completed_count, total_count and remaining_count=0. status='blocked' requires the real blocker and a resumable checkpoint. This tool records completion state but performs no external action.")
def report_task_status(
    status: str,
    summary: str,
    evidence: str,
    completed_count: int = -1,
    total_count: int = -1,
    remaining_count: int = -1,
) -> dict[str, object]:
    """Record an explicit, machine-checkable terminal state for long tasks."""
    normalized = (status or "").strip().lower()
    summary = (summary or "").strip()
    evidence = (evidence or "").strip()
    if normalized not in {"completed", "blocked"}:
        return {"success": False, "error": "status 必须是 completed 或 blocked"}
    if not summary or not evidence:
        return {"success": False, "error": "summary 和 evidence 不能为空"}

    counts = (completed_count, total_count, remaining_count)
    has_counts = any(value >= 0 for value in counts)
    if has_counts and any(type(value) is not int or value < 0 for value in counts):
        return {"success": False, "error": "提供计数时三个计数字段都必须是非负整数"}
    if normalized == "completed" and has_counts:
        if remaining_count != 0 or completed_count != total_count:
            return {
                "success": False,
                "error": "completed 要求 remaining_count=0 且 completed_count=total_count",
            }
    if normalized == "blocked" and has_counts and completed_count + remaining_count != total_count:
        return {
            "success": False,
            "error": "blocked 计数要求 completed_count + remaining_count = total_count",
        }
    return {
        "success": True,
        "terminal": True,
        "status": normalized,
        "summary": summary[:1000],
        "evidence": evidence[:2000],
        "completed_count": completed_count if has_counts else None,
        "total_count": total_count if has_counts else None,
        "remaining_count": remaining_count if has_counts else None,
    }


# ── Registration ─────────────────────────────────────────────────────────

_AGENT_TOOL_NAMES = ("run_python", "pip_install", "run_command", "report_task_status")


def register_agent_tools() -> int:
    """Register every agent_base tool above under its own toolset."""
    from tools.registry import registry, tool_result
    from .tool_decorator import _build_schema

    count = 0
    for name in _AGENT_TOOL_NAMES:
        func = globals().get(name)
        meta = getattr(func, "_tool_meta", None) if callable(func) else None
        if not isinstance(meta, dict):
            logger.warning("agent tool %s missing @tool metadata — skipped", name)
            continue
        registry.register(
            name=name,
            toolset=AGENT_TOOLSET,
            schema=_build_schema(func),
            handler=lambda args, _func=func, **_kw: tool_result(_func(**args)),
            override=True,
        )
        count += 1
    logger.info("Agent base tools registered: %d (python=%s)", count, _resolve_python()[0])
    return count
