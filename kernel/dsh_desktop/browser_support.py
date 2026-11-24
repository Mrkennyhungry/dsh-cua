"""Shared browser launch settings without legacy tool registration."""
import logging
import os
from pathlib import Path
import shutil
import threading

from .config import dsh_desktop_home

logger = logging.getLogger(__name__)
_CDP_PORT = int(os.environ.get('DSH_DESKTOP_CDP_PORT', '9333'))

def _browser_mode() -> str:
    """返回受控浏览器类型；默认 Edge，必要时可设 `DSH_DESKTOP_BROWSER=chromium` 回退。"""
    mode = os.environ.get("DSH_DESKTOP_BROWSER", "edge").strip().lower()
    if mode not in {"edge", "chromium"}:
        raise RuntimeError("DSH_DESKTOP_BROWSER 仅支持 edge 或 chromium")
    return mode


def _browser_label() -> str:
    return "Microsoft Edge" if _browser_mode() == "edge" else "Playwright Chromium"


def _edge_executable() -> str:
    configured = os.environ.get("DSH_DESKTOP_EDGE_EXECUTABLE", "").strip()
    candidates = [
        configured,
        shutil.which("msedge") or "",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError(
        "未找到 Microsoft Edge；请安装 Edge 或设置 DSH_DESKTOP_EDGE_EXECUTABLE 为 msedge.exe 路径"
    )


def _profile_dir():
    # 不与历史 Playwright Chromium 共用 profile，避免浏览器版本/锁文件冲突。
    name = "web_profile_edge" if _browser_mode() == "edge" else "web_profile"
    override = os.environ.get('DSH_DESKTOP_BROWSER_PROFILE')
    d = Path(override) if override else dsh_desktop_home() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prepare_playwright_temp_dir() -> Path:
    """Give the Playwright driver a temp directory writable by the kernel account.

    The elevated scheduled-task kernel can run as a different Windows account
    from the desktop user. In that case the inherited per-user TEMP may exist
    but reject directory creation (Playwright uses mkdtemp for launch artifacts).
    """
    temp_dir = dsh_desktop_home() / "playwright_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    probe = temp_dir / f".write-check-{os.getpid()}-{threading.get_ident()}"
    try:
        probe.write_text("", encoding="ascii")
    finally:
        probe.unlink(missing_ok=True)
    # The Node driver and its browser children inherit these values at startup.
    os.environ["TEMP"] = str(temp_dir)
    os.environ["TMP"] = str(temp_dir)
    return temp_dir


def _launch_persistent_context(pw):
    """owner 线程启动持久 context（带 remote-debugging 口供他线程 CDP 附着）。"""
    common = dict(
        headless=False,
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
    )
    profile = str(_profile_dir())
    launch_options = dict(common)
    if _browser_mode() == "edge":
        launch_options["executable_path"] = _edge_executable()
    try:
        return pw.chromium.launch_persistent_context(
            profile,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=PasswordLeakDetection,PasswordManagerOnboarding,PasswordManagerPasswordReuseDetection",
                "--password-store=basic",
                f"--remote-debugging-port={_CDP_PORT}",
                # 压制"恢复页面?"气泡：它出现在右上角，会视觉遮挡业务 toast
                # （如"分组名称已存在"），干扰 AI 视觉判断
                "--hide-crash-restore-bubble",
                # 浏览器窗口默认最小化运行：防止 Chromium 把"最小化/被遮挡"当成
                # 后台页节流 rAF/定时器（会让页面等待、动画、轮询变慢甚至卡住），
                # 关闭节流后最小化跑用例与前台跑行为一致。
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
            **launch_options,
        )
    except Exception as exc:
        # 端口被占（如另一个 dsh_desktop 实例）时降级为无 CDP 口启动：
        # AI 兜底跨线程复用退化为独立浏览器，但不阻塞主流程
        logger.warning("CDP 端口 %d 启动失败，降级无 CDP 启动: %s", _CDP_PORT, exc)
        return pw.chromium.launch_persistent_context(
            profile,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=PasswordLeakDetection,PasswordManagerOnboarding,PasswordManagerPasswordReuseDetection",
                "--password-store=basic",
                "--hide-crash-restore-bubble",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
            **launch_options,
        )

