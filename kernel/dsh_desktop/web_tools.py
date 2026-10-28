"""Playwright-powered DOM-level web automation (web_* tools).

Desktop vision (desktop_analyze/UIA) works on browsers too, but DOM-level control
is far more reliable for login flows, forms and lists: real selectors, no OCR,
no coordinates. Ported from external automation agent agent_v2 (perceive/web_backend.py +
act/playwright_act.py) and the web-case-gen skill SOP.

Key behaviours inherited from agent_v2:
- One persistent Chromium context (profile dir under DSH_DESKTOP_HOME/web_profile) —
  logins/cookies survive restarts; visible (headful) so the user can watch and
  take over at any time.
- Selectors MUST come from a fresh web_snapshot (never from memory/screenshots).
- Structured failure diagnosis (multi-match / zero-match / invisible) so the
  LLM can retry with a better selector by itself.

Thread note: Playwright sync objects are thread-bound. The owner thread lazily
starts the persistent context WITH a remote-debugging port; any OTHER thread
(e.g. the AI fallback session) attaches to the SAME browser over CDP
(same architecture as UATA desktop_console) — pages/login state are shared, the
failure-scene tab survives the handover instead of being killed by a restart.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from .config import dsh_desktop_home
from .tool_decorator import tool

logger = logging.getLogger(__name__)

WEB_TOOLSET = "desktop_web_tools"

_pw_playwright: Any = None
_pw_context: Any = None
_pw_thread_id: int | None = None
_web_lock = threading.RLock()
_active_page: Any = None  # runner 钉住的活跃页签（AI 兜底优先复用）
_active_page_url: str = ""  # 跨线程（CDP 连接）代理对象不同，按 URL 匹配
_CDP_PORT = int(os.environ.get("DSH_DESKTOP_CDP_PORT", "9333"))
_browser_minimized = False  # 浏览器窗口是否已最小化（默认全程收起，页面截图取证不受影响）
_cdp_tls = threading.local()  # 每线程一条 CDP 连接（pw/browser/ctx）


# ── 页面指纹与新鲜度（宪法 R14；借鉴 jev-ultrafast 的 fingerprint 思想）──────
#
# 每个动作型 web_* 工具的成功结果都携带 page_sig（URL+可见文本头的 hash），
# eternity 编排层据此检测「连续多次动作页面无变化」的死循环并熔断；
# 动作前比对 URL（scheme+host+path 口径）实现快照新鲜度校验——快照后页面
# 已导航时旧 selector 大概率失效，提醒模型重新 web_snapshot 而不是盲点。

_PAGE_SIG_JS = (
    "(() => location.href + '\\n' + "
    "(document.body ? document.body.innerText.slice(0, 1500) : ''))()"
)
_last_page_sig: str | None = None  # 最近一次观察/动作后的页面签名
_last_page_url: str | None = None  # 上一次观察/动作后的完整 URL


def _site_path(url: str) -> str:
    """URL 的 scheme://host/path 口径（query/hash 变化不算导航）。"""
    m = re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*(?:/[^?#]*)?", url or "")
    return (m.group(0).rstrip("/") if m else (url or "").rstrip("/"))


def _page_fingerprint(page: Any) -> tuple[str, str]:
    """轻量页面签名 (sig, url)：单次 evaluate（URL + body 文本前 1500 字符）取 hash，~10ms。"""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    try:
        raw = page.evaluate(_PAGE_SIG_JS) or ""
    except Exception:
        raw = url
    sig = hashlib.sha1(f"{url}\n{raw}".encode("utf-8", "ignore")).hexdigest()[:12]
    return sig, url


def _note_page_state(page: Any, result: dict) -> str:
    """动作/快照成功后调用：刷新基线并在结果里带 page_sig（供熔断检测）。"""
    global _last_page_sig, _last_page_url
    sig, url = _page_fingerprint(page)
    _last_page_sig, _last_page_url = sig, url
    result["page_sig"] = sig
    return sig


def _stale_nav_warning(page: Any) -> dict | None:
    """动作前新鲜度校验：上次观察后页面发生过跨路径导航 → 警告对象。

    只在「scheme+host+path」变化时触发；同页 query/hash 变化（切 tab、翻页）
    属正常操作不算 stale。返回值并入动作结果（不阻断执行——selector 是否
    仍有效由 Playwright 定位本身裁决，这里只补上下文）。
    """
    if not _last_page_url:
        return None
    try:
        cur = page.url or ""
    except Exception:
        return None
    if _site_path(cur) == _site_path(_last_page_url):
        return None
    return {
        "stale_nav": True,
        "observed_url": _last_page_url,
        "current_url": cur,
        "hint": ("自上次 web_snapshot 后页面已导航（URL 路径变化），旧 selector 可能已失效；"
                 "本次结果仅代表执行时状态，继续操作前请重新 web_snapshot。"),
    }


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
    d = dsh_desktop_home() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _context_up_to_date() -> bool:
    global _pw_thread_id
    if _pw_context is None:
        return False
    import threading as _t
    if _pw_thread_id != _t.get_ident():
        return False  # Playwright objects are thread-bound
    try:
        if _pw_context is None or getattr(_pw_context, "_closed", False):
            return False
        # 实际探测：浏览器进程被外部杀掉时，context 对象不会自动标记 _closed。
        # 只摸 .pages 不够（返回缓存 [] 不抛错，实测 mode=launched 但 0 个进程），
        # 必须查 browser.is_connected()。
        br = getattr(_pw_context, "browser", None)
        if br is not None:
            try:
                if not br.is_connected():
                    return False
            except Exception:
                return False
        _ = _pw_context.pages
        return True
    except Exception:
        return False


def _shutdown_context() -> None:
    global _pw_context, _pw_playwright, _pw_thread_id
    try:
        if _pw_context is not None:
            _pw_context.close()
    except Exception:
        pass
    try:
        if _pw_playwright is not None:
            _pw_playwright.stop()
    except Exception:
        pass
    _pw_context = None
    _pw_playwright = None
    _pw_thread_id = None


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


def _get_context():
    """Return (context, page), launching/restarting the browser when needed.

    他线程调用（AI 兜底会话等）不重启浏览器，而是 CDP 附着到同一实例——
    重启会杀掉失败现场页签，AI 只能从 about:blank 从头来。
    """
    global _pw_playwright, _pw_context, _pw_thread_id
    with _web_lock:
        import threading as _t
        if (
            _pw_context is not None
            and _pw_thread_id is not None
            and _pw_thread_id != _t.get_ident()
        ):
            try:
                return _cdp_attach_context()
            except RuntimeError:
                # CDP 附着失败 = 没有共享浏览器在跑（可能被外部关掉，或内核刚重启）。
                # 清掉失效状态后**继续往下走启动路径**，不要 raise ——
                # 之前这里直接抛错，导致内核重启后 web_* 完全不可用（踩过）。
                # 注意：web 调用都跑在 _WEB_EXECUTOR("dsh-web") 线程里，
                # 不在 asyncio 事件循环，启动 Playwright 是安全的。
                logger.info("CDP attach failed; falling through to launch a fresh context")
                _shutdown_context()
        if not _context_up_to_date() or (_pw_context is None and _pw_thread_id is None):
            # 新 CLI 进程没有 owner-thread 内存状态时，优先附着已有共享浏览器。
            # 否则它会尝试二次打开同一 persistent profile，并因 profile lock 崩溃；
            # 更不能把这种基础设施故障误报为业务用例失败。
            if _pw_context is None:
                try:
                    return _cdp_attach_context()
                except RuntimeError:
                    pass  # 未运行共享浏览器时才创建 owner context。
            _shutdown_context()
            _prepare_playwright_temp_dir()
            from playwright.sync_api import sync_playwright
            _pw_playwright = sync_playwright().start()
            try:
                _pw_context = _launch_persistent_context(_pw_playwright)
            except Exception as exc:
                _shutdown_context()
                raise RuntimeError(f"Playwright 启动浏览器失败: {exc}") from exc
            _pw_thread_id = _t.get_ident()
            logger.info(
                "Web browser context started (%s, profile=%s, cdp=:%d)",
                _browser_label(), _profile_dir(), _CDP_PORT,
            )
            # 启动即收起（默认最小化跑用例）：不抢焦点、不遮挡 desktop 客户端，
            # 页面截图取证不受影响；桌面步骤与用例收尾也会再确保一次。
            minimize_browser()
        pages = [p for p in _pw_context.pages if not p.is_closed()]
        page = pages[-1] if pages else _pw_context.new_page()
        # 默认保持窗口最小化：控制台/keepalive 的取证截图是 page.screenshot()
        # （页面截图，与窗口可见性无关），缩小不丢证据，又永远不会盖住 desktop 客户端。
        # 需要可见时由调用方显式 restore_browser()。
        return _pw_context, page


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


def _close_thread_cdp() -> None:
    """断开当前线程的 CDP 连接（不影响共享浏览器本体）。"""
    for attr in ("browser", "pw"):
        obj = getattr(_cdp_tls, attr, None)
        if obj is None:
            continue
        try:
            if hasattr(obj, "stop"):
                obj.stop()
            else:
                obj.close()
        except Exception:
            pass
        setattr(_cdp_tls, attr, None)
    _cdp_tls.ctx = None


def _cdp_attach_context(*, ensure_page: bool = True):
    """CDP 附着到 owner 线程的浏览器（本线程缓存一条连接）。

    对齐 UATA desktop_console 的 CDP attach 架构：playwright sync API 线程
    绑定，跨线程直接复用上下文会崩；CDP 每线程独立连接、页面全共享。
    """
    cached = getattr(_cdp_tls, "ctx", None)
    if cached is not None:
        try:
            pages = [p for p in cached.pages if not p.is_closed()]
            return cached, (pages[-1] if pages else (cached.new_page() if ensure_page else None))
        except Exception:
            # owner 重启过浏览器，本线程连接已失效 → 重连
            _close_thread_cdp()
    from playwright.sync_api import sync_playwright
    # start() 必须在 try 内：同一线程若已有未停止的 sync_playwright，
    # 这里会抛 "Sync API inside the asyncio loop"（评审 N1，实测确认）。
    # 放在 try 外则 pw 未赋值、清理不执行，泄漏会让后续每次调用都失败。
    pw = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{_CDP_PORT}", timeout=10000,
        )
        if _browser_mode() == "edge":
            session = browser.new_browser_cdp_session()
            try:
                product = str(session.send("Browser.getVersion").get("product", ""))
            finally:
                session.detach()
            if "Edg/" not in product:
                browser.close()
                raise RuntimeError(f"CDP 端口 {_CDP_PORT} 当前不是 Microsoft Edge: {product}")
        ctxs = browser.contexts
        if not ctxs:
            raise RuntimeError("浏览器无可用 context")
    except Exception as exc:
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass
        raise RuntimeError(f"CDP 附着浏览器失败(port={_CDP_PORT}): {exc}") from exc
    ctx = ctxs[0]
    _cdp_tls.pw = pw
    _cdp_tls.browser = browser
    _cdp_tls.ctx = ctx
    logger.info("CDP attached to shared browser (port=%d)", _CDP_PORT)
    pages = [p for p in ctx.pages if not p.is_closed()]
    return ctx, (pages[-1] if pages else (ctx.new_page() if ensure_page else None))


def _set_browser_window_state(state: str) -> bool:
    """通过 CDP 设置受控浏览器窗口状态（minimized / normal）。

    确定性实现，不靠 win32 猜窗口句柄：Browser.getWindowForTarget →
    Browser.setWindowBounds。浏览器没在跑时安全返回 False（不会顺手拉起）。
    """
    with _web_lock:
        try:
            ctx = _pw_context
            if ctx is None:
                try:
                    ctx, _ = _cdp_attach_context()
                except RuntimeError:
                    return False
            pages = [p for p in ctx.pages if not p.is_closed()]
            if not pages:
                return False
            cdp = ctx.new_cdp_session(pages[-1])
            try:
                info = cdp.send("Browser.getWindowForTarget")
                window_id = int(info.get("windowId", 0) or 0)
                if not window_id:
                    return False
                cdp.send("Browser.setWindowBounds", {
                    "windowId": window_id,
                    "bounds": {"windowState": state},
                })
                return True
            finally:
                try:
                    cdp.detach()
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("设置浏览器窗口状态(%s)失败: %s", state, exc)
            return False


def minimize_browser() -> bool:
    """最小化受控浏览器窗口（做客户端/桌面步骤前调用）。

    为什么必须做：浏览器是普通 z-order 的可见窗口，会整块压住 desktop 客户端，
    对客户端区域取图/OCR 只能拿到浏览器白页（about:blank），客户端状态识别
    必然失败（知识库 20260915_185811 的现场证据链：desktop 被 about:blank 的 Edge
    完全压住 → OCR 0 texts → 0c「确保处于本地账号登录页」整步失败）。
    相比"强前台化 + 持续 TOPMOST 去硬压"，把浏览器收起来更省事也更确定性。
    """
    global _browser_minimized
    if _browser_minimized:
        return True
    ok = _set_browser_window_state("minimized")
    if ok:
        _browser_minimized = True
        logger.info("已最小化受控浏览器窗口（避免遮挡 desktop 客户端）")
    return ok


def restore_browser() -> bool:
    """还原受控浏览器窗口（需要 web 操作/页面截图时；未最小化时为空操作）。"""
    global _browser_minimized
    if not _browser_minimized:
        return True
    ok = _set_browser_window_state("normal")
    if ok:
        _browser_minimized = False
        logger.info("已还原受控浏览器窗口")
    return ok


def cleanup_case_tabs(keep_page=None) -> None:
    """用例结束时关闭历史页签，仅保留一张空白受控页，并把浏览器窗口最小化。

    保留 about:blank 是有意的（下次用例别从残留页面开始），但窗口不该继续摆在
    最上层遮挡 desktop 客户端——所以收尾时顺手最小化；下次真正需要 web 操作时
    `_get_context` 会自动还原。
    """
    global _active_page, _active_page_url
    with _web_lock:
        try:
            ctx, page = _get_context()
            keeper = keep_page if keep_page is not None and not keep_page.is_closed() else page
            for candidate in list(ctx.pages):
                if candidate is not keeper and not candidate.is_closed():
                    candidate.close()
            if not keeper.is_closed() and keeper.url != "about:blank":
                keeper.goto("about:blank", wait_until="commit")
            _active_page = keeper
            _active_page_url = "about:blank"
            minimize_browser()
        except Exception as exc:
            logger.warning("用例页签清理失败: %s", exc)


def set_active_page(page) -> None:
    """钉住 runner 当前操作的页签：AI 兜底会话的 web 工具将优先复用它，
    从 runner 的现场继续，而非误落到 pages[-1]（可能是空白页签）。
    同时记录 URL：跨线程 CDP 连接的页面对象代理不同，按 URL 匹配。"""
    global _active_page, _active_page_url
    _active_page = page
    if page is None:
        _active_page_url = ""
        return
    try:
        _active_page_url = page.url or ""
    except Exception:
        _active_page_url = ""


def _pick_page(url_contains: str = ""):
    ctx, page = _get_context()
    pages = [p for p in ctx.pages if not p.is_closed()]
    # 优先 runner 钉住的活跃页签（QclCaseRunner 每次动作后 set_active_page），
    # 保证 AI 兜底会话从 runner 的当前页签继续，而非 pages[-1] 误入空白页签
    if _active_page is not None and _active_page in pages:
        if not url_contains or url_contains in _active_page.url:
            return _active_page
    # 跨线程（CDP 连接）时代理对象不同，按 URL 精确匹配 runner 钉住的页签
    if _active_page_url:
        for p in pages:
            if p.url == _active_page_url and (
                not url_contains or url_contains in p.url
            ):
                return p
    if url_contains:
        for p in pages:
            if url_contains in p.url:
                return p
    if page in pages:
        return page
    return pages[-1] if pages else ctx.new_page()


# ── snapshot: interactive-element harvest (the DOM 'vision' for the LLM) ──

_SELECTOR_JS = r"""
  const selectorFor = (el, tag, text, cssOnly = false) => {
    let sel = '';
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
      sel = '#' + CSS.escape(el.id);
    } else if (el.getAttribute('data-testid')) {
      sel = `[data-testid="${CSS.escape(el.getAttribute('data-testid'))}"]`;
    } else if (tag === 'input' && el.getAttribute('name')) {
      sel = `input[name="${CSS.escape(el.getAttribute('name'))}"]`;
    } else if (tag === 'input' && el.getAttribute('placeholder')) {
      sel = `input[placeholder*="${CSS.escape(el.getAttribute('placeholder').slice(0, 24))}"]`;
    } else if (el.getAttribute('aria-label')) {
      sel = `${tag}[aria-label*="${CSS.escape(el.getAttribute('aria-label').slice(0, 24))}"]`;
    }
    if (!cssOnly && !sel && text && text.length <= 40 && text !== '[submit]' && !text.startsWith('[') && !text.startsWith('@')) {
      // Some custom controls need a scoped text selector; a bare text selector may not be clickable.
      const firstClass = (typeof el.className === 'string' ? el.className : '')
        .trim().split(/\s+/).filter(Boolean)[0] || '';
      let scope = '';
      const host = el.closest('[class*="drawer"], [class*="modal"], [class*="dialog"], [class*="panel"]');
      if (host) {
        const hostClass = (typeof host.className === 'string' ? host.className : '')
          .trim().split(/\s+/).filter(Boolean)[0] || '';
        if (hostClass) scope = `${host.tagName.toLowerCase()}.${CSS.escape(hostClass)} `;
      }
      if (firstClass) sel = `${scope}${tag}.${CSS.escape(firstClass)}:has-text(${JSON.stringify(text.slice(0, 20))})`;
      if (!sel) sel = `text=${JSON.stringify(text.slice(0, 30))}`;
    }
    if (!sel) {
      const classes = typeof el.className === 'string' ? el.className.trim().split(/\s+/).filter(Boolean) : [];
      sel = tag + classes.slice(0, 2).map(c => '.' + CSS.escape(c)).join('');
    }
    if (cssOnly) {
      // R1：selector 太长不可用（实测 bing 中位 187 字符）。优先序：#id → 属性 →
      // tag.class（逐级加类取首个唯一解）→ 锚定短路径 → nth-path（省略 html/body 前缀）。
      const isUnique = (m) => m.length === 1 && m[0] === el;
      let matches = document.querySelectorAll(sel);
      if (!isUnique(matches)) {
        const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).filter(Boolean) : [];
        for (let n = 1; n <= cls.length && !isUnique(matches); n++) {
          const cand = tag + cls.slice(0, n).map(c => '.' + CSS.escape(c)).join('');
          const m = document.querySelectorAll(cand);
          if (isUnique(m)) { sel = cand; matches = m; }
        }
      }
      if (!isUnique(matches)) {
        // 从最近的带 id 祖先向下锚定；只有同标签并列才用 nth-of-type，
        // 且始终省略 html:nth-of-type(1) > body:nth-of-type(1) 这类废话前缀。
        const segment = (node) => {
          const type = node.tagName.toLowerCase();
          if (node.id) return '#' + CSS.escape(node.id);
          const nodeCls = typeof node.className === 'string' ? node.className.trim().split(/\s+/).filter(Boolean) : [];
          const base = nodeCls.length ? type + '.' + CSS.escape(nodeCls[0]) : type;
          const peers = node.parentElement ? Array.from(node.parentElement.children).filter(p => p.tagName === node.tagName) : [node];
          if (peers.length === 1) return base;
          const sameCls = peers.filter(p => {
            const pc = typeof p.className === 'string' ? p.className.trim().split(/\s+/).filter(Boolean) : [];
            return nodeCls.length && pc[0] === nodeCls[0];
          });
          if (sameCls.length <= 1) return base;
          return `${type}:nth-of-type(${peers.indexOf(node) + 1})`;
        };
        const parts = [];
        for (let node = el; node && node !== document.body && node !== document.documentElement; node = node.parentElement) {
          parts.unshift(segment(node));
          const cand = parts.join(' > ');
          const m = document.querySelectorAll(cand);
          if (isUnique(m) || node.id) { sel = cand; matches = m; break; }
        }
        if (!isUnique(matches)) {
          // 最终兜底：完整结构路径，但同样省略 html/body。
          const full = [];
          for (let node = el; node && node !== document.body && node !== document.documentElement; node = node.parentElement) {
            const type = node.tagName.toLowerCase();
            const peers = node.parentElement ? Array.from(node.parentElement.children).filter(p => p.tagName === node.tagName) : [node];
            full.unshift(`${type}:nth-of-type(${peers.indexOf(node) + 1})`);
          }
          sel = full.join(' > ');
        }
      }
    }
    return sel;
  };
"""

_SNAPSHOT_JS = "() => {\n" + _SELECTOR_JS + """
  // 自绘组件（div/li/a 实现，没有 role）必须按类名补进来，否则它们既不在快照里、
// 也进不了决策层：自绘页签、翻页与开关可能没有原生 ARIA role，
// 开关是 div[class*=switch]。这三类都实测在控制台里存在但原来一个都看不到。
  const INTERACTIVE = 'button, a[href], input, select, textarea, summary, [role="button"], [role="tab"], [role="menuitem"], [role="option"], [role="link"], [role="checkbox"], [role="radio"], [role="switch"], [onclick], [aria-label]:not([aria-hidden="true"]), [aria-haspopup], [class*="icon" i][role], [class*="tabs__tabitem"], [class*="pagination__turnbtn"], [class*="pagination"] [class*="page"], [class*="switch" i]:not([class*="switch-"]), [class*="dropdown__header"], [class*="dropdown-btn"], [class*="dropdown__value"], [class*="select__trigger"], [class*="select__value"]';
  const OVERLAY_HINT = /modal|dialog|drawer|popup|dropdown|popover|select|overlay|mask|lightbox|ant-modal|tc-modal/i;

  // Deep collect: document.querySelectorAll does NOT pierce shadow DOM —
  // Tencent Cloud console (QDReact) renders dialogs inside shadow roots,
  // which made web_snapshot completely blind to open dialogs (2026-09-07
  // live session: agent clicked 新增账户, dialog opened, snapshot showed
  // zero dialog elements and a wrong truncated:false, so the agent
  // abandoned web mode for slow desktop OCR).
  const deepQueryAll = (root) => {
    const result = [];
    const seen = new Set();
    const push = (el) => {
      if (!seen.has(el)) { seen.add(el); result.push(el); }
    };
    try {
      for (const el of root.querySelectorAll(INTERACTIVE)) push(el);
      // 第二遍：捞委托点击的组件模式——裸 <li> 选项（如自绘下拉菜单
      // 里 <li>添加分组/编辑分组/删除分组</li>，无 role 无 onclick，React
      // 事件委托）和常见 option/menu 类名。没有这遍，弹出的下拉菜单对快照
      // 完全隐形（agent 只好转昂贵的视觉兜底）。
      for (const el of root.querySelectorAll(
        'li[class*="option" i], [class*="menu-item" i], [class*="list--option" i] li, [class*="dropdown" i] li'
      )) push(el);
      for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) {
          for (const sub of deepQueryAll(el.shadowRoot)) push(sub);
        }
      }
    } catch (e) {}
    return result;
  };

  // Cross-boundary parent walk (shadow host aware).
  const parentOf = (node) => {
    if (node.parentElement) return node.parentElement;
    const root = node.getRootNode && node.getRootNode();
    return (root && root.host) ? root.host : null;
  };

  const visibleEl = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return !(style.display === 'none' || style.visibility === 'hidden'
             || rect.width < 2 || rect.height < 2);
  };

  // Is this element inside a currently-visible overlay/dialog?
  const inOpenOverlay = (el) => {
    let node = el;
    for (let i = 0; node && i < 40; i++) {
      if (node.nodeType === 1) {
        let role = '', ariaModal = '', cls = '';
        try {
          role = node.getAttribute('role') || '';
          ariaModal = node.getAttribute('aria-modal') || '';
          cls = (typeof node.className === 'string') ? node.className : '';
        } catch (e) {}
        if (role === 'dialog' || role === 'alertdialog' || ariaModal === 'true'
            || OVERLAY_HINT.test(cls)) {
          try {
            const st = window.getComputedStyle(node);
            if (st.display !== 'none' && st.visibility !== 'hidden') return true;
          } catch (e) {}
        }
      }
      node = parentOf(node);
    }
    return false;
  };

  const describe = (el) => {
    const rect = el.getBoundingClientRect();
    const r = document.documentElement;
    if (rect.bottom < 0 || rect.right < 0 || rect.top > r.clientHeight + 400
        || rect.right > r.clientWidth + 400) return null;  // off-screen
    const tag = el.tagName.toLowerCase();
    let text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
    if (tag === 'input') {
      const t = el.getAttribute('type') || 'text';
      // placeholder 要进 text：goal="搜索终端" 只能靠它匹配（输入框本身没有可见文本）。
      // 注意这个控制台的 placeholder 常是兄弟节点 <p class="...placeholder">，
      // 不在 input 上，所以要就近往上找三层。
      let label = el.getAttribute('placeholder') || '';
      if (!label) {
        let host = el.parentElement;
        for (let depth = 0; depth < 3 && host && !label; depth++, host = host.parentElement) {
          const holder = host.querySelector('[class*="placeholder" i]');
          const value = holder && (holder.textContent || '').trim();
          if (value) label = value;
        }
      }
      text = `[${t}]` + (label ? ` ${String(label).slice(0, 30)}` : '')
             + (el.value ? ` value=${String(el.value).slice(0, 30)}` : '');
    }
    // 图标按钮（纯 <i>/<span> 无文本，如三个点 i[aria-label="Pop_icon_gd"]）：
    // 用 aria-label 当显示名，否则快照里是一行空 text 无法辨认
    if (!text) {
      const al = el.getAttribute('aria-label') || '';
      const title = el.getAttribute('title') || '';
      const alt = (el.querySelector('img[alt]') || {}).alt || el.getAttribute('alt') || '';
      const named = al || title || alt;
      if (named) text = '@' + String(named).slice(0, 40);
      else {
        const xlink = el.querySelector && el.querySelector('use');
        const href = xlink && (xlink.getAttribute('xlink:href') || xlink.getAttribute('href')) || '';
        if (href) text = '@' + href.replace(/^#?icon-/, '').slice(0, 40);
      }
    }
    const sel = selectorFor(el, tag, text);
    return {
      tag, text: text || '', selector: sel,
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      role: el.getAttribute('role') || '',
      // bbox 给决策层与截图核对用；CSS px，相对视口左上角
      rect: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)],
      href: tag === 'a' ? String(el.getAttribute('href') || '').slice(0, 60) : '',
      // Component signature for the catalog layer: the class names are the only
      // stable DOM contract these design systems expose (some pages have no
      // data-testid / aria contract), so keep the first few, deduped.
      cls: (typeof el.className === 'string' ? el.className : '')
             .trim().split(/\\s+/).filter(Boolean).slice(0, 4),
    };
  };

  const all = deepQueryAll(document);
  const overlayOut = [], regularOut = [];
  let hitCap = false, seen = 0;
  const CAP = 200, REGULAR_CAP = 120;
  for (const el of all) {
    seen++;
    if (!visibleEl(el)) continue;
    const d = describe(el);
    if (!d) continue;
    if (inOpenOverlay(el)) {
      d.in_overlay = true;
      if (overlayOut.length < 80) overlayOut.push(d);
    } else if (regularOut.length < REGULAR_CAP) {
      regularOut.push(d);
    } else {
      hitCap = true;  // regular bucket full — but KEEP SCANNING: dialog
                      // portals sit at the end of body and must still be
                      // collected (breaking here was the original blindness).
    }
  }
  // Overlay elements are PREPENDED: when a dialog is open, the LLM sees its
  // 确定/取消/表单控件 first instead of 120 table rows that pushed them out.
  const elements = overlayOut.concat(regularOut).slice(0, CAP);
  return {
    elements,
    dialog_open: overlayOut.length > 0,
    overlay_count: overlayOut.length,
    hit_cap: hitCap || (elements.length >= CAP),
    total_interactive_seen: seen,
  };
}
"""


@tool("采集当前网页的可交互元素清单（DOM 级，text/selector）。浏览器操作前必采集；点击、切换 tab/radio、打开抽屉后必须重新采集。selector 只准来自这里，禁止凭记忆或截图猜。重复调用成本极低，但同一页面如已采集过且 DOM 未变，可直接复用上次结果。")
def web_snapshot(max_elements: int = 60, include_classes: bool = False) -> dict:
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": f"浏览器未就绪: {exc}"}
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        # Iframes: consoles host the app inside one (某些控制台会将应用放在
        # iframe 里). page.evaluate only sees the main frame, so a dialog rendered
        # by the embedded app was completely invisible — collect every frame and
        # tag each element with its frame index so click/type can target it.
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
        merged: list[dict] = []
        hit_cap = False
        used_frames = 0
        for index, frame in enumerate(frames):
            try:
                nodes = frame.evaluate(_SNAPSHOT_JS) or {}
            except Exception:  # noqa: BLE001 - a detached/cross-origin frame is skippable
                continue
            frame_elements = nodes.get("elements") or []
            if not frame_elements:
                continue
            used_frames += 1
            hit_cap = hit_cap or bool(nodes.get("hit_cap"))
            for element in frame_elements:
                if index:
                    element["frame"] = index
                merged.append(element)
        nodes = {"hit_cap": hit_cap, "elements": merged, "frames": used_frames}
        elements = merged
        limit = max(10, min(int(max_elements), 200))
        truncated = bool(nodes.get("hit_cap")) or len(elements) > limit

        def _slim(el: dict) -> dict:
            """瘦身元素：selector+text+弹窗标记足矣。

            旧版 7 字段（tag/text/selector/id/placeholder/href/…）× 60 元素
            每次快照 ~3-4K token，agent 又被要求每次点击后重新快照，是 AI
            兜底会话上下文膨胀到 65K+（单次 prefill 30s+）的主因。

            include_classes=True 时额外带 tag/cls：组件目录（web_catalog）需要类名
            才能归因到设计系统，但那会显著增加 token，所以默认关闭。
            """
            out = {"selector": str(el.get("selector", ""))[:80]}
            text = str(el.get("text") or "").strip()
            if text:
                out["text"] = text[:40]
            if include_classes:
                # 只有采集路径（组件目录 / 覆盖率）需要坐标：判断下拉选项属于
                # 哪个刚打开的下拉框要靠 rect 邻近，否则会把顶栏的选项混进来。
                # 默认模式不带，省 token。
                rect = el.get("rect") or []
                if len(rect) == 4:
                    out["rect"] = [int(v) for v in rect]
            if el.get("in_overlay"):
                out["in_overlay"] = True
            if el.get("frame"):
                # Selectors are frame-scoped: the caller must pass frame back when
                # clicking, otherwise the locator resolves in the main document and
                # silently matches nothing.
                out["frame"] = int(el["frame"])
            if include_classes:
                out["tag"] = str(el.get("tag") or "")
                classes = el.get("cls") or []
                if classes:
                    out["cls"] = [str(name)[:40] for name in classes][:4]
            return out

        # nth 必须在去重**之前**按出现顺序编号，且去重键要带上位置：
        # 顶栏"根域"和抽屉里的"根域"共用同一个选择器和文本，只按 (selector,text)
        # 去重会把后者丢掉，于是两个都是 nth=0，调用方永远点不到抽屉里那个。
        _assign_nth(elements[:limit])
        slimmed: list[dict] = []
        seen: set[tuple] = set()
        for el in elements[:limit]:
            s = _slim(el)
            s["nth"] = int(el.get("nth") or 0)
            key = (s.get("frame"), s.get("selector"), s.get("text"),
                   tuple((el.get("rect") or [])[:2]))
            if key in seen:
                continue
            seen.add(key)
            slimmed.append(s)
        result = {
            "success": True,
            "url": page.url,
            "title": page.title(),
            "count": len(slimmed),
            "truncated": truncated,
            "elements": slimmed,
            "hint": "直接把 elements 里的 selector 传给 web_click/web_type。"
                    "带 in_overlay 的元素属于当前弹窗/对话框（已排在最前），优先处理。",
        }
        if nodes.get("dialog_open"):
            # Make the open dialog impossible to miss — this is what the
            # agent needed on 2026-09-07 to stay in web mode.
            result["dialog_open"] = True
            result["overlay_count"] = nodes.get("overlay_count", 0)
        _note_page_state(page, result)  # 快照即观察基线：刷新 sig/url 供 stale 校验与熔断
        return result


# ── structured failure diagnosis (ported from playwright_act.py) ─────────

def _diagnose(page: Any, selector: str) -> str:
    try:
        count = page.locator(selector).count()
    except Exception as exc:
        return f"selector 非法: {exc.__class__.__name__}"
    if count == 0:
        return f"零匹配：selector={selector!r} 不在当前 DOM（页面可能已变化，请重新 web_snapshot）"
    if count == 1:
        try:
            loc = page.locator(selector).first
            if not loc.is_visible():
                return f"元素存在但不可见：selector={selector!r}（可能被折叠/隐藏）"
            bb = loc.bounding_box()
            if bb and (bb["width"] == 0 or bb["height"] == 0):
                return f"元素尺寸为零：selector={selector!r}"
            return (f"元素可见但操作失败（很可能被弹窗/对话框遮挡）：selector={selector!r} rect={bb}。"
                    "请重新 web_snapshot——若 dialog_open=true，弹窗元素会带 in_overlay 排在最前，"
                    "先处理弹窗（填写/确定/取消）再继续原操作")
        except Exception as exc:
            return f"状态检查失败: {exc}"
    rows: list[str] = []
    try:
        loc = page.locator(selector)
        for i in range(min(count, 5)):
            try:
                text = (loc.nth(i).inner_text(timeout=800) or "").strip().replace("\n", " ")[:50]
            except Exception:
                text = ""
            rows.append(f"[{i}] text={text!r}")
    except Exception:
        pass
    return f"多匹配：selector={selector!r} 命中 {count} 个元素，需加容器前缀精确定位。" + "；".join(rows)


_MAX_SEL = 300


def _clean_selector(selector: str) -> str:
    return (selector or "").strip()[:_MAX_SEL]


# ── actions ──────────────────────────────────────────────────────────────

@tool("浏览器打开网址（受控 Chromium，带持久 profile——登录态会保留）。首次打开需人工登录的站点登录一次即可。")
def web_open(url: str, timeout_ms: int = 30000) -> dict:
    global _active_page, _active_page_url
    url = (url or "").strip()
    if not url:
        return {"success": False, "error": "url 不能为空"}
    # Only bare hostnames get https://. Local schemes must pass through
    # untouched: file:///C:/x.html was rewritten to https://file///C:/x.html and
    # failed with ERR_NAME_NOT_RESOLVED (2026-10-27 acceptance run).
    if not re.match(r"^(?:https?|file|about|data|view-source):", url, re.IGNORECASE):
        url = "https://" + url
    with _web_lock:
        # 页面可能已被外部关闭（清场/用户手动关标签）。_pick_page 有兜底，
        # 但拿到的 page 也可能处于 closed 状态，必须显式换一个新的。
        try:
            page = _pick_page()
            if page.is_closed():
                ctx, _ = _get_context()
                page = ctx.new_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=max(5000, min(int(timeout_ms), 60000)))
        except Exception as exc:
            msg = str(exc)
            # "context/browser has been closed" = 僵尸 context（浏览器进程已被外部清掉）。
            # 重置后重建一次；重建不了才如实报错。
            if "has been closed" in msg or "Target closed" in msg:
                logger.info("web_open: zombie context detected, resetting and retrying")
                _shutdown_context()
                try:
                    ctx2, _ = _get_context()
                    page = ctx2.new_page()
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=max(5000, min(int(timeout_ms), 60000)))
                    _active_page = page
                    _active_page_url = page.url
                    return {"success": True, "url": page.url, "title": page.title(),
                            "recovered_from_zombie": True,
                            "hint": "浏览器上下文曾失效，已自动重建。"}
                except Exception as exc2:
                    return {"success": False, "url": url,
                            "error": f"导航失败且重建失败: {exc2}"}
            return {"success": False, "error": f"导航失败: {exc}", "url": url}
        # 必须把新导航的页签钉为 active，否则后续 _pick_page 会靠 _active_page_url
        # 选回旧页签 —— 实测：open(example.com) 后 status 读到的是 edge://nurturing/，
        # 因为浏览器里有两个 page，_pick_page 走了"活动页优先"分支。
        _active_page = page
        _active_page_url = page.url
        return {
            "success": True, "url": page.url, "title": page.title(),
            "hint": "接着 web_snapshot 采集页面元素再操作。",
        }


# Confirm-button semantic equivalence was removed on purpose: auto-clicking a
# different confirm-ish button (确定/确认/保存/提交) when the requested one is
# missing means the agent's stated target is not what gets clicked. A missing
# target now returns diagnostics plus visible candidates, and the caller decides.


# 会产生副作用的按钮文案：采集阶段一律拒绝，执行阶段必须显式放行且给验证方式。
# 注意「放弃/离开」不在此表 —— 它的语义是"丢弃草稿并关闭"，是退出弹窗的正确路径。
MUTATING_WORDS = ("保存", "提交", "确定", "确认", "应用", "删除", "移除", "下发",
                  "停用", "禁用", "启用", "重置", "清空", "立即创建", "创建",
                  "导入", "上传", "同步", "发布", "审批")


# 这些词在页面上是"打开表单"，在弹窗/抽屉内部却是"提交落库"（实测：认证策略抽屉
# 底部的按钮就叫「添加」）。所以要结合所在容器判断，不能只看文案。
DIALOG_MUTATING_WORDS = ("添加", "新建", "创建", "完成", "生成")


def mutating_word(label: str, in_dialog: bool = False) -> str:
    """返回命中的副作用词；空串表示不是写操作。

    这是**唯一判定入口**：闸门和审计都调它。曾经闸门用精确匹配、审计用包含匹配，
    结果「添加适用范围」（下拉触发器）被误记进 mutations.jsonl。
    """
    hit = next((word for word in MUTATING_WORDS if word in (label or "")), "")
    if not hit and in_dialog and (label or "").strip() in DIALOG_MUTATING_WORDS:
        hit = (label or "").strip()
    return hit


def mutation_gate(label: str, allow_mutating: bool, verify: str,
                  in_dialog: bool = False) -> dict | None:
    """写操作闸门：返回拒绝结果，或 None 表示可以执行。

    分两层：
      * 采集（默认）：副作用按钮一律拒绝 —— 收益为 0（提交不产生新的界面结构），
        风险不为 0（脏数据、部分不可回滚）。
      * 执行：显式 allow_mutating=True，且**必须**给 verify。
        "没有验证方式的写操作"比"点错按钮"更难查，所以直接不许发生。
    """
    hit = mutating_word(label, in_dialog)
    if not hit:
        return None
    if not allow_mutating:
        return {"success": False, "refused": True, "mutating": True,
                "error": f"写操作被拒绝：「{hit}」属于副作用按钮。"
                         "采集请勿点击；执行需显式传 allow_mutating=true 并提供 verify。"}
    if not (verify or "").strip():
        return {"success": False, "refused": True, "mutating": True,
                "error": f"「{hit}」是写操作，必须提供 verify（提交后用什么信号确认成功，"
                         "例如 toast 文本 / 列表新增 / url_changed）。"}
    return None


@tool("点击页面元素（selector 来自 web_snapshot）。失败时返回结构化诊断（零匹配/多匹配/不可见），按诊断换 selector 重试，不要原样重试超过 2 次。找「保存」不存在时会自动尝试同义确认按钮（确定/确认/提交），并在返回里注明。写操作（保存/提交/确定/删除…）默认被拒绝：采集期请勿点击；执行期需 allow_mutating=True 且提供 verify。")
def web_click(selector: str, timeout_ms: int = 10000, frame: int = 0, nth: int | None = None,
              allow_mutating: bool = False, verify: str = "", owner: str = "",
              commit: str = "", target_text: str = "") -> dict:
    selector = _clean_selector(selector)
    if not selector:
        return {"success": False, "error": "selector 不能为空", "error_code": "BAD_SELECTOR"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc), "error_code": "BROWSER_NOT_READY"}
        stale = _stale_nav_warning(page)
        # 破坏性操作的目标唯一性断言必须在**定位元素之前**做：
        # 否则元素不存在时 wait_for 会先抛错，断言永远轮不到执行，
        # 我们就拿不到"目标不唯一 / 不存在"这种可判读的拒绝理由（实测踩到）。
        try:
            precheck_label = (page.locator(selector).first.inner_text(timeout=800) or "").strip()
        except Exception:
            precheck_label = ""
        if _is_destructive(precheck_label):
            precheck = assert_unique_target(page, precheck_label, target_text)
            if precheck is not None:
                return precheck
        dispatched = False
        click_attempted = False
        try:
            target = page.frames[frame] if frame else page
            loc = target.locator(selector)
            loc.first.wait_for(state="visible", timeout=max(2000, min(int(timeout_ms), 30000)))
            # nth 必须按**可见元素**编号：快照只列可见元素、按顺序编号，
            # 而 Playwright 的 nth 把隐藏元素也算进去。两边口径不一致时，
            # 快照给 nth=0 的元素，实际点到的可能是列表里第一个隐藏项（实测点不到
            # 「编辑」按钮就是这个原因）。
            matches = loc.all()
            visible = []
            for match in matches:
                try:
                    if match.is_visible():
                        visible.append(match)
                except Exception:
                    continue
            hit = None
            if nth is not None:
                if int(nth) < len(visible):
                    hit = visible[int(nth)]
            elif len(visible) == 1:
                hit = visible[0]
            # 写操作闸门：采集阶段（默认）拒绝一切副作用按钮；执行阶段必须显式
            # allow_mutating=True 且给出 verify —— "没有验证方式的写操作"不许发生。
            label = ""
            probe = hit if hit is not None else loc.first
            try:
                label = (probe.inner_text(timeout=2000) or "").strip()
            except Exception:
                label = ""
            # 弹窗/抽屉内部的「添加/新建/创建」是提交落库，页面上的是打开表单。
            # 只看文案会漏掉前者（实测：认证策略抽屉底部按钮就叫「添加」）。
            in_dialog = False
            try:
                probe_el = hit if hit is not None else loc.first
                in_dialog = bool(probe_el.evaluate(
                    "el => !!el.closest('[class*=dialog],[class*=drawer],[class*=modal]')"))
            except Exception:
                in_dialog = False
            # 与闸门同一个判定入口，避免"闸门放行但审计记成写操作"的口径漂移。
            mutating = mutating_word(label, in_dialog)
            gate = mutation_gate(label, allow_mutating, verify, in_dialog=in_dialog)
            if gate is not None:
                return gate
            # 破坏性操作必须证明"目标唯一"：用户约定（先搜索、筛到只剩一条）
            # 由内核强制，而不是指望 agent 每次自觉（我们踩过两次误点）。
            uniqueness = assert_unique_target(page, label, target_text)
            if uniqueness is not None:
                return uniqueness
            # commit="local"：调用方声明这是"表单内提交"（只改本地表单状态、不发请求）。
            # 内核不轻信声明——按所在弹窗的文本校验，含销毁性词就是真落库，直接拒。
            if (commit or "").strip() == "local":
                ok_local, why = is_local_commit(label, _dialog_text(probe_el))
                if not ok_local:
                    return {"success": False, "refused": True,
                            "error": {"code": "COMMIT_DECLARATION_FALSE",
                                      "hint": f"声明 commit=local 不成立：{why}"}}
            # 只要调用方声明了写意图（allow_mutating=True）就必须持租约，
            # 不能只看文案是否命中副作用词：否则"点文案普通的按钮但声明要写"
            # 会绕过租约（验收脚本抓到过这个口子）。
            if allow_mutating or mutating:
                lease_refusal = _lease_guard(owner)
                if lease_refusal is not None:
                    return lease_refusal
            # 同名元素很常见（顶栏的域选择 + 抽屉里的域选择都是 text="根域"）。
            # 快照会给出 nth，调用方按序号点，不再因为 count!=1 直接失败。
            # 点击前抓一次内核指纹：写操作的"变没变"必须由内核自己判断，
            # 不能拿 agent 的 verify 文本当证据（report isolation）。
            _before = _page_evidence(page) if (mutating or allow_mutating) else None
            if hit is not None:
                # Playwright can throw *after* the input events were sent (target
                # closed, navigation interrupted). Record the attempt so the
                # handler below cannot report "element not found" for it.
                click_attempted = True
                hit.click(timeout=int(timeout_ms))
                dispatched = True
            elif nth is not None:
                raise RuntimeError(f"nth={nth} 超出可见元素范围（可见 {len(visible)} 个 / 共 {loc.count()} 个）")
            else:
                raise RuntimeError(f"可见匹配 {len(visible)} 个 / 共 {loc.count()} 个"
                                   "（可用 nth 指定序号）")
            # Everything below runs after the click already reached the page.
            # A failure here must not fall into the handler below, which would
            # otherwise report "element not found" for a click that happened.
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            result = {"success": True, "clicked": selector,
                      "hint": "页面可能已变化：继续操作前重新 web_snapshot。"}
            if mutating:
                result["mutating"] = True
                result["verify"] = verify.strip()
                try:
                    _audit_mutation(page, selector, label, verify, "clicked", before=_before)
                except Exception as exc:  # noqa: BLE001 - 审计失败不该改写点击结果
                    result["audit_error"] = f"{exc.__class__.__name__}: {exc}"
            if stale:
                result.update(stale)
            try:
                _note_page_state(page, result)
            except Exception as exc:  # noqa: BLE001
                result["note_page_state_error"] = f"{exc.__class__.__name__}: {exc}"
            return result
        except Exception as exc:
            reason = f"{exc.__class__.__name__}: {exc}"[:160]
            if dispatched or (click_attempted and "count=" not in reason and "nth=" not in reason):
                # The click was already sent (or the library may have sent it
                # before throwing). We do not know whether the page acted on it,
                # so we must not click anything else and must not claim the
                # action was undone.
                return {"success": False, "error_code": "RESULT_UNKNOWN",
                        "selector": selector, "click_dispatched": bool(dispatched),
                        "click_attempted": bool(click_attempted),
                        "error": f"点击已发出，但随后读取页面状态失败：{reason}",
                        "hint": "结果未知：先 web_snapshot 观察页面实际状态，不要重试点击。"}
            # error_code 直接由底层给出，不指望上层从文本猜（DSH 0062 的要求）。
            code = "AMBIGUOUS_SELECTOR" if "count=" in str(reason) else "ELEMENT_NOT_FOUND"
            return {"success": False, "selector": selector, "error_code": code,
                    "error": f"元素未找到或不可点击：{reason}" if reason else "元素未找到或不可点击",
                    "diagnosis": _diagnose(page, selector)}


@tool("向输入框填入文本（fill，先清空再输入，中文安全）。press_enter=True 时填完补回车（搜索框/登录提交）。")
def web_type(selector: str, text: str, press_enter: bool = False, timeout_ms: int = 10000, frame: int = 0) -> dict:
    selector = _clean_selector(selector)
    if not selector:
        return {"success": False, "error": "selector 不能为空", "error_code": "BAD_SELECTOR"}
    if text is None:
        text = ""
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc), "error_code": "BROWSER_NOT_READY"}
        stale = _stale_nav_warning(page)
        try:
            target = page.frames[frame] if frame else page
            loc = target.locator(selector)
            loc.first.wait_for(state="visible", timeout=max(2000, min(int(timeout_ms), 30000)))
            if loc.count() != 1:
                raise RuntimeError(f"count={loc.count()}")
            loc.first.fill(str(text), timeout=int(timeout_ms))
            if press_enter:
                # 必须发给元素本身，不能用 page.keyboard：受控组件在 onChange 后重渲染，
                # 焦点可能已经不在输入框上，页面级 Enter 就落到 body（实测批量里搜索失效）。
                loc.first.press("Enter", timeout=int(timeout_ms))
            result: dict = {"success": True, "filled": selector, "chars": len(text)}
            if stale:
                result.update(stale)
            _note_page_state(page, result)
            return result
        except Exception as exc:
            code = "AMBIGUOUS_SELECTOR" if "count=" in str(exc) else "ELEMENT_NOT_FOUND"
            return {"success": False, "selector": selector, "error_code": code,
                    "error": str(exc)[:200], "diagnosis": _diagnose(page, selector)}


@tool("选择下拉框选项（原生 <select> 元素）。value 传 option 的 value 或可见文本。自定义组件下拉请改用 web_click 点击。")
def web_select_option(selector: str, value: str, timeout_ms: int = 10000, frame: int = 0) -> dict:
    selector = _clean_selector(selector)
    value = (value or "").strip()
    if not selector or not value:
        return {"success": False, "error": "selector 与 value 均不能为空"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        stale = _stale_nav_warning(page)
        try:
            target = page.frames[frame] if frame else page
            loc = target.locator(selector)
            if loc.count() != 1:
                raise RuntimeError(f"count={loc.count()}")
            try:
                loc.first.select_option(value, timeout=int(timeout_ms))
            except Exception:
                loc.first.select_option(label=value, timeout=int(timeout_ms))
            result = {"success": True, "selected": value, "selector": selector}
            if stale:
                result.update(stale)
            _note_page_state(page, result)
            return result
        except Exception as exc:
            return {"success": False, "selector": selector,
                    "error": str(exc)[:200], "diagnosis": _diagnose(page, selector)}


@tool("在网页上按键（Enter/Escape/Tab/ArrowDown 等）。主要用例：搜索回车、关弹层 Esc。组合键写法 Control+a。")
def web_press_key(key: str) -> dict:
    key_map = {"esc": "Escape", "ctrl": "Control", "del": "Delete", "return": "Enter",
               "ins": "Insert", "arrowup": "ArrowUp", "arrowdown": "ArrowDown",
               "arrowleft": "ArrowLeft", "arrowright": "ArrowRight"}
    key_pw = key_map.get((key or "").strip().lower(), (key or "").strip())
    if not key_pw or len(key_pw) > 40:
        return {"success": False, "error": "key 非法"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            page.keyboard.press(key_pw)
            result = {"success": True, "pressed": key_pw}
            stale = _stale_nav_warning(page)
            if stale:
                result.update(stale)
            _note_page_state(page, result)
            return result
        except Exception as exc:
            return {"success": False, "error": f"按键失败: {exc}"}


@tool("网页滚动（正值向下）。单位为像素。返回滚动前后的 scrollY；frame 参数暂不支持，传非 0 会明确报错。")
def web_scroll(pixels: int = 600, frame: int = 0) -> dict:
    pixels = max(-20000, min(int(pixels), 20000))
    with _web_lock:
        if int(frame) != 0:
            # S5：之前收了 frame 却静默忽略。明确不支持，不让调用方以为滚进了 iframe。
            return {"success": False, "error": "frame 不支持：iframe 内滚动未实现，请把 frame=0 作用于主页面。"}
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            before = page.evaluate("() => window.scrollY")
            page.mouse.wheel(0, pixels)
            after = page.evaluate("() => window.scrollY")
            # 不能只看 success：前后位置是实际位移证据。
            return {"success": True, "scrolled": pixels, "frame": int(frame),
                    "scrollY_before": before, "scrollY_after": after,
                    "moved": after != before}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


@tool("悬停在页面元素上。行内操作按钮（如行尾『三个点』菜单按钮）多为 hover 才显示——快照看不到它们不是不存在，而是未悬停。正确流程：web_hover(行 selector) → 立即 web_snapshot（现在能看到三个点/操作按钮了）→ web_click。")
def web_hover(selector: str, timeout_ms: int = 10000, frame: int = 0) -> dict:
    selector = _clean_selector(selector)
    if not selector:
        return {"success": False, "error": "selector 不能为空"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            target = page.frames[frame] if frame else page
            target.hover(selector, timeout=max(1000, min(int(timeout_ms), 30000)))
            return {
                "success": True,
                "hint": "已悬停。行内按钮已可见：优先用组合选择器直接点击，如 "
                        "web_click(\"<本行selector> >> i[aria-label]\")，"
                        "不依赖快照采集（小图标可能被元素上限截断）；"
                        "或立即 web_snapshot(max_elements=150) 采集后点击。",
            }
        except Exception as exc:
            return {
                "success": False,
                "selector": selector,
                "error": f"悬停失败: {exc}",
                "diagnosis": _diagnose(page, selector),
            }


@tool("提取页面/元素的文本内容（元素内列表、表格、详情）。selector 省略时提取 body 全文（截断 4000 字）。")
def web_extract(selector: str = "", max_chars: int = 4000, frame: int = 0) -> dict:
    max_chars = max(200, min(int(max_chars), 20000))
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            if selector.strip():
                sel = _clean_selector(selector)
                target = page.frames[frame] if frame else page
                loc = target.locator(sel)
                if loc.count() == 0:
                    return {"success": False, "selector": sel,
                            "error": "零匹配", "diagnosis": _diagnose(page, sel)}
                text = loc.first.inner_text(timeout=8000)
            else:
                text = page.inner_text("body", timeout=10000)
        except Exception as exc:
            return {"success": False, "error": f"提取失败: {exc.__class__.__name__}"}
        text = (text or "").strip()
        return {"success": True, "url": page.url, "chars": len(text),
                "text": text[:max_chars], "truncated": len(text) > max_chars}


@tool("等待元素出现/可见（打开新页签、异步加载后用）。selector 来自最近的 web_snapshot。")
def web_wait(selector: str, timeout_ms: int = 15000) -> dict:
    selector = _clean_selector(selector)
    if not selector:
        return {"success": False, "error": "selector 不能为空"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            page.locator(selector).first.wait_for(
                state="visible", timeout=max(1000, min(int(timeout_ms), 60000)))
            return {"success": True, "appeared": selector, "url": page.url}
        except Exception:
            return {"success": False, "selector": selector,
                    "error": f"等待 {timeout_ms}ms 后仍未出现",
                    "diagnosis": _diagnose(page, selector)}


@tool("执行只读 JS 表达式（如取 document.title、读某个变量的文本）。禁止修改页面/发请求的代码；输出截断 4000 字。")
def web_eval_js(expression: str) -> dict:
    expression = (expression or "").strip()[:8000]
    if not expression:
        return {"success": False, "error": "expression 不能为空"}
    lowered = expression.lower()
    if any(k in lowered for k in ("fetch(", "xmlhttprequest", "localstorage.setitem", "document.write")):
        return {"success": False, "error": "仅支持只读表达式（禁止 fetch/XHR/写入）"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            result = page.evaluate(expression)
        except Exception as exc:
            return {"success": False, "error": f"JS 执行失败: {str(exc)[:200]}"}
        text = result if isinstance(result, str) else repr(result)
        return {"success": True, "result": text[:4000]}


@tool("查看受控浏览器状态（是否在运行、打开的页签列表）。")
def web_status() -> dict:
    """报告浏览器状态，区分「内核自己拉起的实例」和「CDP 附着的外部实例」。

    之前只报 running=_pw_context 是否为空，于是通过 CDP 附着的外部 Edge
    （9333 + junction 那套方案）会被误报成"未运行"，调用方看到 false 就会
    自己 web_open —— 而 Playwright 拉起的浏览器正是被登录风控拦的那种。
    """
    with _web_lock:
        attached_ctx = getattr(_cdp_tls, "ctx", None)
        if _pw_context is None and attached_ctx is None:
            return {"success": True, "running": False, "attached": False, "mode": "none",
                    "hint": "web_open 可启动受控浏览器，或先起一个带 --remote-debugging-port 的 Edge（CDP 会自动附着）。"}
        ctx = _pw_context if _pw_context is not None else attached_ctx
        mode = "launched" if _pw_context is not None else "cdp"
        try:
            pages = [{"url": p.url, "title": p.title()} for p in ctx.pages if not p.is_closed()]
        except Exception:
            pages = []
        return {"success": True, "running": True, "attached": mode == "cdp", "mode": mode,
                "cdp_port": _CDP_PORT, "tabs": pages}


def web_lifecycle_snapshot() -> dict[str, Any]:
    """Capture the controlled browser state without launching a browser.

    Called on the dedicated dsh-web thread at task start. If a managed browser
    survived a kernel restart, attach over CDP so it is recognized as a
    pre-existing resource and is not closed as a task-created window.
    """
    with _web_lock:
        ctx = _pw_context or getattr(_cdp_tls, "ctx", None)
        if ctx is None:
            try:
                ctx, _ = _cdp_attach_context(ensure_page=False)
            except Exception:
                return {"running": False, "page_keys": []}
        try:
            pages = [page for page in ctx.pages if not page.is_closed()]
        except Exception:
            pages = []
        return {"running": True, "page_keys": [str(id(page)) for page in pages]}


def web_lifecycle_cleanup(snapshot: dict[str, Any] | None, *, keep_browser: bool = False) -> dict[str, Any]:
    """Release only browser resources created after the task baseline.

    This must run on the same dsh-web thread as Playwright operations.
    Existing contexts stay warm; pages created by this task are closed unless
    the caller explicitly asked to keep the browser state.
    """
    baseline = snapshot or {"running": False, "page_keys": []}
    with _web_lock:
        ctx = _pw_context or getattr(_cdp_tls, "ctx", None)
        if keep_browser:
            return {"status": "kept", "closed_pages": 0}
        if not baseline.get("running"):
            if ctx is None:
                return {"status": "not_running", "closed_pages": 0}
            result = web_close()
            note = str(result.get("note", ""))
            if result.get("success") and "已关闭" in note:
                return {"status": "closed", "closed_pages": 0}
            if result.get("success") and "未在运行" in note:
                return {"status": "not_running", "closed_pages": 0}
            return {"status": "failed", "closed_pages": 0, "detail": note or result.get("error", "")}
        if ctx is None:
            return {"status": "preserved", "closed_pages": 0}
        baseline_pages = {str(key) for key in baseline.get("page_keys", [])}
        closed = 0
        failed = []
        try:
            pages = [page for page in ctx.pages if not page.is_closed()]
        except Exception as exc:
            return {"status": "failed", "closed_pages": 0, "detail": f"{type(exc).__name__}: {exc}"}
        for page in pages:
            if str(id(page)) in baseline_pages:
                continue
            try:
                page.close()
                closed += 1
            except Exception as exc:
                failed.append(f"{page.url}: {type(exc).__name__}: {exc}")
        return {"status": "failed" if failed else "preserved", "closed_pages": closed,
                **({"detail": "; ".join(failed)} if failed else {})}


@tool("关闭受控浏览器（profile 与登录态保留在磁盘，下次 web_open 恢复）。")
def web_close() -> dict:
    global _pw_context
    with _web_lock:
        import threading as _t
        if _pw_thread_id is not None and _pw_thread_id != _t.get_ident():
            # 他线程（AI 会话收尾）调用：只断开本线程 CDP 连接，
            # 共享浏览器保留（runner 可能还要继续用失败现场之后的页面）
            _close_thread_cdp()
            return {"success": True, "note": "已断开本线程 CDP 连接（共享浏览器保留）"}
        if _pw_context is None:
            return {"success": True, "note": "浏览器未在运行"}
        _shutdown_context()
        return {"success": True, "note": "已关闭；登录态已保留"}


# ── Registration ─────────────────────────────────────────────────────────

def register_web_tools() -> int:
    """Register every @tool function in this module under the desktop_web_tools toolset."""
    from tools.registry import registry, tool_result
    from .tool_decorator import _build_schema

    count = 0
    for name, func in list(globals().items()):
        if not name.startswith("web_") or not callable(func):
            continue
        meta = getattr(func, "_tool_meta", None)
        if not isinstance(meta, dict):
            continue
        schema = _build_schema(func)
        registry.register(
            name=name,
            toolset=WEB_TOOLSET,
            schema=schema,
            handler=lambda args, _func=func, **_kw: tool_result(_func(**args)),
            override=True,
        )
        count += 1
    logger.info("Web tools registered: %d", count)
    return count

_DOM_QUERY_JS = """
(args) => {
  const { query, limit } = args;
  const out = [];
  let nodes = [];
  // 穿透 shadow DOM：document.querySelectorAll 看不到 shadow root 里的节点，
  // 而这个控制台把应用渲染在 shadow root 里，导致 6 个不同页面被盘点成同一套
  // 组件族（外壳 sdk-* 全在 light DOM，应用内容一个都看不到）。
  const deepMatches = (root, selector) => {
    const found = [];
    const seen = new Set();
    const push = (node) => { if (!seen.has(node)) { seen.add(node); found.push(node); } };
    try {
      for (const el of root.querySelectorAll(selector)) push(el);
      for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) {
          for (const sub of deepMatches(el.shadowRoot, selector)) push(sub);
        }
      }
    } catch (e) { }
    return found;
  };
  try { nodes = deepMatches(document, query); } catch (e) { return { error: 'bad_selector' }; }
  for (const el of nodes.slice(0, limit)) {
    const rect = el.getBoundingClientRect();
    const nodeTag = el.tagName.toLowerCase();
    out.push({
      tag: nodeTag,
      cls: (typeof el.className === 'string' ? el.className : '').trim().split(/\s+/).filter(Boolean).slice(0, 5),
      text: (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60),
      role: el.getAttribute('role') || '',
      aria: el.getAttribute('aria-label') || '',
      // 输入框的值必须单独取：innerText 对 input 恒为空，而"填完值对不对"
      // 是最常用的 verify 信号（没有它只能靠截图猜）。
      value: (nodeTag === 'input' || nodeTag === 'textarea' || nodeTag === 'select')
             ? String(el.value === undefined ? '' : el.value).slice(0, 80) : '',
      // 与快照同一套可见性判据：只看尺寸不够——导航后拦截页/弹窗的 DOM 会留在
      // 文档里（display:none 或 visibility:hidden），尺寸仍然非零，会被误计数。
      visible: (() => {
        try {
          const st = window.getComputedStyle(el);
          if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
        } catch (e) { }
        if (rect.width < 2 || rect.height < 2) return false;
        // 必须在视口内：下拉选项残留在 portal 里时坐标可能是 y=-9600 这种
        // 视口外的值，只判 display/size 会把它当"可见"，污染选项采集。
        const vh = window.innerHeight || 0, vw = window.innerWidth || 0;
        if (rect.bottom < 0 || rect.top > vh || rect.right < 0 || rect.left > vw) return false;
        return true;
      })(),
      rect: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)],
    });
  }
  return { count: nodes.length, elements: out };
}
"""


def web_dom_query(query: str, limit: int = 40, frame: int = 0) -> dict:
    """结构化的 DOM 查询：给一个 CSS 选择器，回元素摘要（tag/class/text/rect）。

    刻意只接受选择器、不接受任意 JS —— eval_js 是故意不暴露的。组件目录需要
    看"这个类名在 DOM 里长什么样"，而快照的可选列表覆盖不了自绘组件（tea 的
    Select 没有 role，就是一个 div），所以需要这一层。
    """
    query = (query or "").strip()
    if not query or len(query) > 300:
        return {"success": False, "error": "query 不能为空且不超过 300 字符"}
    # :has-text() / :text-is() 是 Playwright 引擎语法，document.querySelectorAll 不支持。
    # 直接吃进去会静默返回 0 个元素，把"查询语法不对"误判成"元素不存在"——
    # 实测因此让一次回放 verify 假失败。这里显式拦住。
    for pseudo in (":has-text(", ":text-is(", ":text(", ">>"):
        if pseudo in query:
            return {"success": False,
                    "error": f"dom 查询用 CSS 选择器，不支持 {pseudo!r}。"
                             "文本请用纯 CSS 取回后在调用方过滤，"
                             "例如 selector='[class*=\"dropdown__header\"]' 再筛 text。"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        target = page.frames[frame] if frame else page
        try:
            result = target.evaluate(_DOM_QUERY_JS, {"query": query, "limit": max(1, min(int(limit), 200))})
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{exc.__class__.__name__}: {exc}"}
        if not isinstance(result, dict) or result.get("error"):
            return {"success": False, "error": "选择器无效"}
        return {"success": True, "query": query, "count": result.get("count", 0),
                "elements": result.get("elements", [])}
_FRAME_PROBE_JS = """
() => {
  const count = document.querySelectorAll('*').length;
  const cls = Array.from(document.querySelectorAll('[class]')).slice(0, 200)
    .map((el) => (typeof el.className === 'string' ? el.className : ''))
    .join(' ').split(/\\s+/).filter(Boolean);
  const prefix = {};
  for (const name of cls) {
    const head = name.split(/[-_]/)[0].slice(0, 12);
    prefix[head] = (prefix[head] || 0) + 1;
  }
  return { elements: count, top_prefixes: prefix };
}
"""


def web_frames(max_frames: int = 20) -> dict:
    """列出页面里的所有帧及其内容概况。

    为什么需要：控制台把应用放在 iframe / 微前端容器里时，主帧的 DOM 查询只能看到
    外壳（sdk-* 类名），应用内容一个都看不到——看起来像"页面没有内容"。这个动作一次
    就能告出"内容到底在哪一帧"。
    """
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        out = []
        for index, frame in enumerate(page.frames[: max(1, min(int(max_frames), 50))]):
            try:
                url = frame.url or ""
            except Exception:  # noqa: BLE001
                url = ""
            info = {"index": index, "url": url[:200], "elements": None, "top_prefixes": {}}
            try:
                probe = frame.evaluate(_FRAME_PROBE_JS)
                if isinstance(probe, dict):
                    info["elements"] = probe.get("elements")
                    info["top_prefixes"] = probe.get("top_prefixes") or {}
            except Exception as exc:  # noqa: BLE001 - detached frame is informative too
                info["error"] = f"{exc.__class__.__name__}"
            out.append(info)
        return {"success": True, "count": len(out), "frames": out,
                "hint": "有内容的帧会给出 elements 与 top_prefixes；主帧通常只有外壳（sdk-*）"}

_EVIDENCE_JS = """() => {
  const count = document.querySelectorAll('*').length;
  const body = document.body ? document.body.innerText : '';
  // 只改输入框时 innerText 不变，所以 value 必须一起进 hash（踩过：名称已填但判成没变）
  const values = [...document.querySelectorAll('input,select,textarea')]
    .map((el) => (el.name || el.placeholder || el.className) + ':' + (el.value || ''))
    .join('|');
  const active = document.activeElement;
  return {
    count,
    blob: body + '\\u0000' + values,
    active: active ? (active.tagName || '') + '.' + (active.className || '') : '',
  };
}"""


def web_evidence() -> dict:
    """batch 用的便宜证据：全页元素数 + 文本/表单值 hash + 焦点元素。

    刻意不截图：默认不该为了一次"变没变"再花一份图片 token。
    """
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}
        try:
            data = page.evaluate(_EVIDENCE_JS)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{exc.__class__.__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"success": False, "error": "evidence 返回异常"}
    blob = str(data.get("blob") or "")
    return {
        "success": True,
        "elements": int(data.get("count") or 0),
        "text_hash": hashlib.sha1(blob.encode("utf-8", "ignore")).hexdigest()[:16],
        "active_element": str(data.get("active") or "")[:60],
    }


def web_decide_action(goal: str, max_elements: int = 200) -> dict:
    """只读的决策预览：给出 decide 会选哪个元素，但不执行任何动作。

    先只读、再可执行 —— 否则第一次跑就是拿真实页面做实验。
    """
    from .web_decide import decide_web

    snapshot = web_snapshot(max_elements=max_elements, include_classes=True)
    if not snapshot.get("success"):
        return {"success": False, "action": "decide", "error": snapshot.get("error") or "snapshot failed"}
    elements = snapshot.get("elements") or []
    result = decide_web(goal, elements)
    return {"success": True, "action": "decide", "tool": "web_decide",
            "goal": goal, "url": snapshot.get("url"),
            "elements_seen": len(elements), **result}

def _assign_nth(elements: list[dict]) -> list[dict]:
    """给每个元素标上同 selector 组内的序号，让调用方能用 nth 精确定位。

    控制台里同名元素很普遍（顶栏"根域"和抽屉里的"根域"是同一个 text 选择器），
    没有序号就只能靠猜，或者直接失败。
    """
    seen: dict[str, int] = {}
    for element in elements:
        selector = element.get("selector") or ""
        index = seen.get(selector, 0)
        element["nth"] = index
        seen[selector] = index + 1
    return elements

def _page_evidence(page) -> dict:
    """独立于 agent 的页面指纹：元素数 + 可见文本 hash（含 input 值）。

    审计要能回答"页面到底变了吗"，而这件事**必须由内核自己算**——
    agent 声称什么不能作为证据（ICSE 2027《Who Verifies the Agent's Success Claim?》
    实测：去掉 agent 自述后，对危险误报的召回率提升 13–43 个百分点）。
    """
    try:
        return page.evaluate(
            """() => {
                 const els = document.querySelectorAll('*');
                 let text = document.body ? (document.body.innerText || '') : '';
                 const vals = [];
                 for (const el of document.querySelectorAll('input,select,textarea')) {
                   vals.push(String(el.value === undefined ? '' : el.value));
                 }
                 const raw = text + '\u0001' + vals.join('\u0001');
                 // FNV-1a：便宜、无依赖、足以判断"变没变"
                 let h = 0x811c9dc5;
                 for (let i = 0; i < raw.length; i++) {
                   h ^= raw.charCodeAt(i);
                   h = (h * 0x01000193) >>> 0;
                 }
                 return { elements: els.length, text_hash: h.toString(16), url: location.href };
               }""")
    except Exception:
        return {}


def _audit_mutation(page, selector: str, label: str, verify: str, result: str,
                    before: dict | None = None) -> None:
    """把每一次写操作追加到审计日志（claim 与 evidence 分开存）。

    真实环境里的写操作必须留痕：谁点的、点的哪个元素、**声称**要验证什么、
    以及**内核自己观测到**页面发生了什么。

    分两栏的理由（report isolation）：agent 的自述会污染成功判定。所以
      claim    = agent 说的话（未经验证，仅供参考）
      evidence = 内核自己测到的前后态（判定只应吃这一栏）
    """
    try:
        from pathlib import Path as _Path
        import datetime as _dt
        import json as _json

        after = _page_evidence(page)
        evidence = {}
        if before and after:
            evidence = {
                "elements_before": before.get("elements"),
                "elements_after": after.get("elements"),
                "text_hash_before": before.get("text_hash"),
                "text_hash_after": after.get("text_hash"),
                "text_hash_changed": before.get("text_hash") != after.get("text_hash"),
                "url_before": before.get("url"),
                "url_after": after.get("url"),
                "url_changed": before.get("url") != after.get("url"),
            }

        home = _Path.home() / ".dsh" / "desktop"
        home.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": _dt.datetime.now().isoformat(timespec="seconds"),
            "url": getattr(page, "url", ""),
            "selector": selector,
            "label": label[:60],
            "result": result,
            # ── claim：agent 声称的验证方式（未经验证） ──
            "claim": {"verify": verify.strip(), "verified_by_kernel": False},
            # ── evidence：内核独立观测（判定只应看这一栏） ──
            "evidence": evidence,
            # 兼容旧字段（历史上按扁平结构写的，读日志的工具还在用）
            "verify": verify.strip(),
        }
        with (home / "mutations.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 审计失败不能阻断业务动作

@tool("勾选/取消勾选复选框（Playwright check/uncheck）。按元素定位，不依赖坐标，"
      "适用于表单里的复选框、开关的 input 本体。")
def web_check(selector: str, nth: int | None = None, checked: bool = True,
              timeout_ms: int = 10000, frame: int = 0) -> dict:
    selector = _clean_selector(selector)
    if not selector:
        return {"success": False, "error": "selector 不能为空"}
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            target = page.frames[frame] if frame else page
            loc = target.locator(selector)
            loc.first.wait_for(state="attached", timeout=max(2000, min(int(timeout_ms), 30000)))
            matches = loc.all()
            visible = []
            for match in matches:
                try:
                    if match.is_visible():
                        visible.append(match)
                except Exception:
                    continue
            if nth is not None:
                if int(nth) >= len(visible):
                    return {"success": False, "selector": selector,
                            "error": f"nth={nth} 超出可见元素范围（可见 {len(visible)} / 共 {len(matches)}）"}
                hit = visible[int(nth)]
            elif len(visible) == 1:
                hit = visible[0]
            else:
                return {"success": False, "selector": selector,
                        "error": f"可见匹配 {len(visible)} 个 / 共 {len(matches)} 个（用 nth 指定）"}
            if checked:
                hit.check(timeout=int(timeout_ms))
            else:
                hit.uncheck(timeout=int(timeout_ms))
            try:
                state = hit.is_checked()
            except Exception:
                state = None
            return {"success": True, "selector": selector, "nth": nth,
                    "checked": state, "hint": "勾选后继续操作前重新 web_snapshot。"}
        except Exception as exc:
            return {"success": False, "selector": selector,
                    "error": f"勾选失败：{exc.__class__.__name__}: {str(exc)[:120]}",
                    "diagnosis": _diagnose(page, selector)}

_SETTLE_JS = "() => document.querySelectorAll('*').length"


@tool("等页面渲染稳定：轮询元素总数，连续 quiet_ms 不变才返回。"
      "点击后立刻查询会拿到旧状态（实测多次踩到），此后动作建议先 settle 再判断。")
def web_set_files(selector: str, files: list[str], timeout_ms: int = 15000,
                  frame: int = 0, nth: int | None = None) -> dict:
    """给 <input type=file> 设置文件 —— 上传文件的正确做法。

    为什么不用模拟"打开文件"对话框：那是系统原生对话框，坐标点击极不可靠，
    而且不同系统/主题下布局不同。Playwright 的 set_input_files 是 DOM 级操作，
    直接把文件路径交给 input 元素，不经过任何对话框。
    """
    import os as _os

    sel = (selector or "").strip()
    if not sel:
        return {"success": False, "error": "selector 不能为空"}
    paths = [str(_os.path.abspath(f)) for f in (files or [])]
    if not paths:
        return {"success": False, "error": "files 不能为空"}
    missing = [f for f in paths if not _os.path.isfile(f)]
    if missing:
        return {"success": False, "error": f"文件不存在: {missing[:2]}"}

    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": f"浏览器未就绪: {exc}"}
        try:
            target = page
            if frame:
                frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
                if frame < len(frames):
                    target = frames[frame]
            loc = target.locator(sel) if hasattr(target, "locator") else target
            if nth is not None:
                loc = loc.nth(int(nth))
            loc.first.set_input_files(paths, timeout=max(3000, min(int(timeout_ms), 60000)))
        except Exception as exc:
            return {"success": False, "error": f"set_input_files 失败: {exc}"}
        return {
            "success": True, "selector": sel, "files": paths, "count": len(paths),
            "note": "已通过 DOM 设置文件；不会弹系统对话框。",
        }


_READ_JS = "(options) => {\n" + _SELECTOR_JS + """
  const interactive = [], seen = new Set();
  let scanned = 0;
  for (const el of document.querySelectorAll('input,textarea,select,button,a[href],[role=button],[contenteditable]')) {
    if (interactive.length >= options.max_elements) break;
    scanned++;
    const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
    if (seen.has(el) || style.display === 'none' || style.visibility === 'hidden' || !rect.width || !rect.height) continue;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    interactive.push({tag, type: el.getAttribute('type') || (tag === 'input' ? 'text' : ''),
      selector: selectorFor(el, tag, (el.innerText || '').trim(), true),
      placeholder: el.getAttribute('placeholder') || '',
      value: el.type === 'password' ? '' : String(el.value || ''), visible: true});
  }
  return {url: location.href, title: document.title,
    text: options.include_text && document.body ? document.body.innerText.slice(0, options.max_text_chars) : '',
    headings: Array.from(document.querySelectorAll('h1,h2,h3')).map(el => el.innerText.trim()).filter(Boolean),
    interactive, elements_scanned: scanned};
}
"""


def web_read(max_elements: int = 80, include_text: bool = True, max_text_chars: int = 3000) -> dict:
    started = time.monotonic()
    with _web_lock:
        try:
            context = getattr(_cdp_tls, "ctx", None) or _pw_context
            if context is None:
                raise RuntimeError("browser not running；请先 web_open，读取不会启动浏览器")
            browser = context.browser
            if browser is not None and not browser.is_connected():
                raise RuntimeError("browser not running；浏览器连接已关闭")
            pages = [page for page in context.pages if not page.is_closed()]
            if not pages:
                raise RuntimeError("没有可读取的页面；请先 web_open")
            page = _active_page if _active_page in pages else next(
                (p for p in pages if _active_page_url and p.url == _active_page_url), pages[-1])
            data = page.evaluate(_READ_JS, {"max_elements": max(1, min(int(max_elements), 200)),
                                         "include_text": bool(include_text),
                                         "max_text_chars": max(0, min(int(max_text_chars), 20000))})
            return {"success": True, **data, "duration_ms": round((time.monotonic() - started) * 1000)}
        except Exception as exc:
            return {"success": False, "error": f"WEB_READ_FAILED: {type(exc).__name__}: {exc}",
                    "hint": "确认浏览器和页面仍在运行；本入口不会启动、附着或重建浏览器。",
                    "duration_ms": round((time.monotonic() - started) * 1000)}


def web_peek_snapshot(max_elements: int = 60, include_classes: bool = False) -> dict:
    """只读观察：浏览器没在运行时**绝不** attach 或 launch。

    为什么需要（评审 N2）：`/observe` 会顺带采 DOM，而走 web_snapshot
    会经 _get_context() 在需要时把浏览器拉起来 —— 观察就产生了副作用，
    还会在 FastAPI 线程池里碰 Playwright（跨线程）。
    观察只应该看已经存在的东西。
    """
    running = (_pw_context is not None) or (getattr(_cdp_tls, "ctx", None) is not None)
    if not running:
        return {"success": False, "available": False,
                "error": "browser not running",
                "note": "观察不会启动浏览器；需要 DOM 时先 web_open"}
    return web_snapshot(max_elements, include_classes)


def web_settle(quiet_ms: int = 600, timeout_ms: int = 8000, frame: int = 0) -> dict:
    quiet = max(100, min(int(quiet_ms), 5000))
    limit = max(500, min(int(timeout_ms), 30000))
    with _web_lock:
        try:
            page = _pick_page()
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        target = page.frames[frame] if frame else page
        started = time.time()
        last, stable_since = -1, time.time()
        samples = 0
        while (time.time() - started) * 1000 < limit:
            try:
                total = int(target.evaluate(_SETTLE_JS) or 0)
            except Exception:
                return {"success": False, "error": "页面不可读（可能正在导航）"}
            samples += 1
            now = time.time()
            if total != last:
                last, stable_since = total, now
            elif (now - stable_since) * 1000 >= quiet:
                return {"success": True, "elements": last,
                        "quiet_ms": quiet, "elapsed_ms": int((now - started) * 1000),
                        "samples": samples}
            time.sleep(0.15)
        return {"success": True, "elements": last, "timed_out": True,
                "elapsed_ms": int((time.time() - started) * 1000), "samples": samples}

# ── 单写者租约 ────────────────────────────────────────────────────────────
# 为什么需要：同一个浏览器被两个会话同时驱动时，页面会互相打断（切 tab、开关抽屉），
# 更糟的是产生"陈旧 DOM"——两个写者交替操作后，双方看到的都可能是过期状态
# （实测两次：一次误判"已删除"，一次误判"残留还在"）。
# 约定：读动作不需要租约；写动作必须持有。TTL 滑动续期，内核重启即失效（安全默认）。
_LEASE: dict = {"owner": None, "expires_at": 0.0, "acquired_at": 0.0}
_LEASE_LOCK = threading.Lock()
LEASE_TTL_S = 120.0


def _lease_now() -> float:
    return time.monotonic()


def web_lease(op: str = "status", owner: str = "", ttl_s: float = LEASE_TTL_S,
              force: bool = False) -> dict:
    """单写者租约：acquire / renew / release / status。"""
    ttl = max(10.0, min(float(ttl_s), 3600.0))
    owner = (owner or "").strip()
    with _LEASE_LOCK:
        now = _lease_now()
        held = _LEASE["owner"]
        alive = bool(held) and now < float(_LEASE["expires_at"])
        remaining = int(float(_LEASE["expires_at"]) - now) if alive else 0

        if op == "status":
            return {"success": True, "held": alive, "owner": held if alive else None,
                    "expires_in_s": remaining, "ttl_s": int(ttl)}

        if not owner:
            return {"success": False, "error": "owner 不能为空",
                    "error": {"code": "OWNER_REQUIRED", "hint": "owner 不能为空"}}

        if op == "acquire":
            if alive and held != owner:
                return {"success": False, "held": False, "holder": held,
                        "expires_in_s": remaining, "error_code": "LEASE_HELD",
                        "hint": f"桌面正被 {held} 占用（剩余 {remaining}s）。"
                                "可以等待、让对方 release、或只跑只读动作。"}
            _LEASE.update(owner=owner, expires_at=now + ttl, acquired_at=now)
            return {"success": True, "held": True, "owner": owner, "expires_in_s": int(ttl)}

        if op == "renew":
            if not alive or held != owner:
                return {"success": False, "held": False, "holder": held if alive else None,
                        "error_code": "LEASE_NOT_HELD",
                        "hint": "续期前必须先 acquire"}
            _LEASE["expires_at"] = now + ttl
            return {"success": True, "held": True, "owner": owner, "expires_in_s": int(ttl)}

        if op == "release":
            if not alive:
                return {"success": True, "held": False, "note": "租约已过期，无需释放"}
            if held != owner and not force:
                return {"success": False, "held": False, "holder": held,
                        "expires_in_s": remaining, "error_code": "NOT_OWNER",
                        "hint": "只能用 owner 释放；强制释放需 force=true（会记审计）"}
            previous = held
            _LEASE.update(owner=None, expires_at=0.0, acquired_at=0.0)
            if force and previous != owner:
                _audit_lease("force_release", previous, owner, remaining)
            return {"success": True, "held": False, "released": previous, "forced": bool(force)}

        return {"success": False, "error": {"code": "BAD_OP", "hint": f"未知 op: {op}"}}


def _audit_lease(action: str, previous: str, by: str, remaining: int) -> None:
    try:
        from pathlib import Path as _Path
        import datetime as _dt
        import json as _json

        home = _Path.home() / ".dsh" / "desktop"
        home.mkdir(parents=True, exist_ok=True)
        entry = {"at": _dt.datetime.now().isoformat(timespec="seconds"),
                 "kind": "lease", "action": action, "previous_owner": previous,
                 "by": by, "remaining_s": remaining}
        with (home / "mutations.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _lease_guard(owner: str) -> dict | None:
    """写动作前的租约校验：返回拒绝结果，或 None 表示放行（并滑动续期）。"""
    owner = (owner or "").strip()
    if not owner:
        return {"success": False, "refused": True, "error_code": "LEASE_REQUIRED",
                "error": "写操作需要单写者租约：请先 POST /web {action:lease, op:acquire, owner:<id>}",
                "hint": "两个会话同时写会互相打断并产生陈旧 DOM，所以写动作必须持租约。"}
    with _LEASE_LOCK:
        now = _lease_now()
        held = _LEASE["owner"]
        alive = bool(held) and now < float(_LEASE["expires_at"])
        if alive and held != owner:
            return {"success": False, "refused": True, "error_code": "LEASE_HELD",
                    "error": f"桌面正被 {held} 占用",
                    "holder": held,
                    "expires_in_s": int(float(_LEASE["expires_at"]) - now)}
        if not alive or held != owner:
            _LEASE.update(owner=owner, expires_at=now + LEASE_TTL_S, acquired_at=now)
        else:
            _LEASE["expires_at"] = now + LEASE_TTL_S   # 滑动续期
        return None

# ── 表单内提交 vs 落库提交 ────────────────────────────────────────────────
# 「确定」有两种语义：
#   1) 弹窗内把选中的对象放回表单（transfer/选择器）—— 只改本地表单状态，不发请求，可撤销
#   2) 删除/停用确认框的「确定」—— 真的发请求改服务端
# 两者文案相同，只能靠"弹窗自身文本里有没有销毁性词"区分。
# 批量里第 1 种不该占用"唯一写步骤"的名额（否则被迫拆批），但它仍然是
# 有副作用的动作：必须 allow_mutating + verify + 审计，且由调用方显式声明 commit="local"，
# 内核在执行时校验声明是否属实（不属实直接失败，不允许静默放宽）。
LOCAL_COMMIT_WORDS = ("确定", "确认", "完成", "下一步")
DESTRUCTIVE_WORDS = ("删除", "移除", "停用", "禁用", "作废", "清空", "重置", "解绑")


def _dialog_text(el) -> str:
    """读元素所在弹窗/抽屉的可见文本（用于区分表单内提交与销毁确认）。"""
    try:
        return el.evaluate(
            "el => { const d = el.closest('[class*=dialog],[class*=drawer],[class*=modal]');"
            " return d ? (d.innerText || '').replace(/\\s+/g, ' ').slice(0, 300) : ''; }")
    except Exception:
        return ""


def is_local_commit(label: str, dialog_text: str) -> tuple[bool, str]:
    """判断是否属于"表单内提交"。返回 (是否, 原因)。"""
    word = (label or "").strip()
    if word not in LOCAL_COMMIT_WORDS:
        return False, f"文案 {word!r} 不是表单内提交词 {LOCAL_COMMIT_WORDS}"
    hit = next((w for w in DESTRUCTIVE_WORDS if w in (dialog_text or "")), "")
    if hit:
        return False, f"所在弹窗含销毁性词 {hit!r}，属真实落库确认"
    return True, ""

# ── 破坏性操作的唯一匹配断言 ───────────────────────────────────────────────
# 用户提的约定："删除前先搜索，筛到只剩这一条再删"。约定本身是对的，
# 但只靠 agent 自觉会失效——我们实际踩过两次：
#   1) 搜索返回「暂无数据」时仍按 nth=0 点击，点到了别的东西
#   2) 按 button 索引点「删除」，结果跳到购买页
# 所以把它做成内核断言：破坏性动作必须声明目标文本，内核验证"该文本在页面上
# 恰好只有 1 个可见匹配，且被点的元素就在它里面/同一行内"。
# 成本：一次 querySelectorAll（十几毫秒），不增加 agent 的思考轮次。
DESTRUCTIVE_ACTIONS = ("删除", "移除", "停用", "禁用", "作废", "清空", "重置", "解绑", "注销")


def _is_destructive(label: str) -> str:
    """返回命中的销毁性词（空串表示不是）。"""
    return next((w for w in DESTRUCTIVE_ACTIONS if w in (label or "")), "")


def assert_unique_target(page, label: str, target_text: str) -> dict | None:
    """破坏性动作的目标唯一性断言。返回 None 表示通过，否则返回拒绝结果。

    target_text 为空 → 拒绝（不允许"我自己心里知道是这条"）。
    """
    word = _is_destructive(label)
    if not word:
        return None
    want = (target_text or "").strip()
    if not want:
        return {
            "success": False, "refused": True,
            "error": {"code": "TARGET_TEXT_REQUIRED",
                      "hint": f"「{label}」是破坏性操作，必须传 target_text（要删除对象的名字/标识），"
                              "内核会验证它在页面上恰好只有一个可见匹配。"},
            "destructive_word": word,
        }
    try:
        found = page.evaluate(
            """(needle) => {
                 const out = [];
                 const els = document.querySelectorAll('body *');
                 for (const el of els) {
                   if (el.children.length > 3) continue;      // 只看叶子级，避免容器整块命中
                   const t = (el.innerText || el.textContent || '');
                   if (!t || t.indexOf(needle) === -1) continue;
                   const r = el.getBoundingClientRect();
                   if (r.width <= 0 || r.height <= 0) continue;
                   const cs = getComputedStyle(el);
                   if (cs.visibility === 'hidden' || cs.display === 'none') continue;
                   out.push({text: t.trim().slice(0, 80), y: r.top, h: r.height});
                 }
                 return out;
               }""", want)
    except Exception as exc:
        return {"success": False, "refused": True,
                "error": {"code": "TARGET_CHECK_FAILED", "hint": f"无法校验目标唯一性: {exc}"}}
    # 同一行的多个单元格都会命中，按 (行 y 区间) 去重后计数
    rows, seen = 0, []
    for item in found or []:
        y = item.get("y") or 0
        if any(abs(y - s) < 8 for s in seen):
            continue
        seen.append(y)
        rows += 1
    if rows == 0:
        return {"success": False, "refused": True,
                "error": {"code": "TARGET_NOT_FOUND",
                          "hint": f"页面上找不到 {want!r}（0 个可见匹配）。先搜索/筛选到目标再操作。"},
                "target_text": want}
    if rows > 1:
        return {"success": False, "refused": True,
                "error": {"code": "TARGET_NOT_UNIQUE",
                          "hint": f"{want!r} 在页面上有 {rows} 个匹配，无法确定要动哪一条。"
                                  "请先搜索/筛选到只剩目标这一条（用户的约定）再重试。"},
                "target_text": want, "matches": rows,
                "samples": [i.get("text") for i in (found or [])][:3]}
    return None
