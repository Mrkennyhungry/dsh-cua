"""Authenticated transport for the unified desktop runtime and optional UI."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from pathlib import Path
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from . import desktop_tools, input_gate

# Captured once at import: the health response must describe loaded code, not
# whatever files happen to be on disk after an update.
_SOURCE_ROOT = Path(__file__).resolve().parent
_BUILD = hashlib.sha256(b"".join((_SOURCE_ROOT / name).read_bytes() for name in
    ("runtime.py", "dsh_bridge.py", "desktop_tools.py", "actions.py", "conditions.py", "observations.py",
     "operations.py", "worker.py", "browser.py", "browser_support.py", "dom.py", "contracts.py",
     "targets.py", "geometry.py", "journal.py", "input_gate.py", "window_identity.py", "version.py"))).hexdigest()[:16]
_INPUT_REVISION = hashlib.sha256((_SOURCE_ROOT / 'game' / 'input.py').read_bytes()).hexdigest()[:16]

class CompanionStartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_pid: int = Field(gt=0)


class CursorStateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    active: bool
    action: str | None = None
    client_pid: int | None = Field(default=None, ge=1)


def create_router(runtime) -> APIRouter:
    token = os.getenv("DSH_DESKTOP_BRIDGE_TOKEN", "")

    def authorize(request: Request) -> str:
        if len(token) < 32:
            raise HTTPException(503, "Bridge disabled: set DSH_DESKTOP_BRIDGE_TOKEN (32+ characters)")
        if request.headers.get("origin"):
            raise HTTPException(403, "Browser-origin requests are not allowed")
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            raise HTTPException(401, "Invalid bridge token")
        owner = request.headers.get("x-dsh-owner", "")
        if len(owner) != 64 or any(c not in "0123456789abcdef" for c in owner):
            raise HTTPException(400, "A SHA256 DSH owner identity is required")
        return owner

    router = APIRouter(prefix="/api/dsh", dependencies=[Depends(authorize)])

    @router.get('/runtime/frames/{frame_id}')
    def runtime_frame(frame_id: str, owner: str = Depends(authorize)):
        from .targets import RuntimeFault
        try:
            data = runtime.observations.image(owner, frame_id)
        except RuntimeFault as error:
            raise HTTPException(404, str(error)) from error
        return Response(content=data, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})

    @router.post('/runtime/observe')
    async def runtime_observe(request: Request, payload: dict, owner: str = Depends(authorize)):
        return await runtime_request(request, owner, 'observe', payload)

    async def runtime_request(request, owner, kind, payload):
        from .operations import Cancelled
        from .targets import RuntimeFault
        if set(payload) != {'request_key', 'arguments'} or not isinstance(payload['request_key'], str) or not payload['request_key']:
            raise HTTPException(422, 'A request_key and arguments are required')
        cancelled = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(getattr(runtime, kind), owner, payload['request_key'],
                                                    payload['arguments'], cancelled=cancelled))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.05)
                if await request.is_disconnected():
                    cancelled.set()
                    break
            return await task
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except RuntimeFault as error:
            return {'success': False, 'error': {'code': error.code, 'message': str(error)}}
        except Cancelled as error:
            raise HTTPException(499, str(error)) from error
        finally:
            if not task.done():
                cancelled.set()

    @router.post('/runtime/open')
    async def runtime_open(request: Request, payload: dict, owner: str = Depends(authorize)):
        return await runtime_request(request, owner, 'open', payload)

    @router.post('/runtime/act')
    async def runtime_act(request: Request, payload: dict, owner: str = Depends(authorize)):
        return await runtime_request(request, owner, 'act', payload)

    @router.post('/runtime/wait')
    async def runtime_wait(request: Request, payload: dict, owner: str = Depends(authorize)):
        return await runtime_request(request, owner, 'wait', payload)

    @router.post('/runtime/stop')
    def runtime_stop(payload: dict, owner: str = Depends(authorize)):
        from .targets import RuntimeFault
        if set(payload) - {'operation_id'}:
            raise HTTPException(422, 'Unknown stop arguments')
        try:
            return runtime.operations.stop(owner, payload.get('operation_id'))
        except RuntimeFault as error:
            return {'success': False, 'error': {'code': error.code, 'message': str(error)}}

    @router.post('/runtime/status')
    async def runtime_status(request: Request, payload: dict, owner: str = Depends(authorize)):
        from .operations import Cancelled
        from .targets import RuntimeFault
        if set(payload) - {'operation_id', 'request_key', 'follow_ms'}:
            raise HTTPException(422, 'Unknown status arguments')
        operation_id = payload.get('operation_id')
        request_key = payload.get('request_key')
        if not operation_id and not request_key:
            from .version import __version__
            return {'success': True, 'epoch': runtime.epoch, 'version': __version__, 'build': _BUILD,
                    'initialized_backends': runtime.initialized_backends(),
                    'workers': {'browser_initialized': runtime.browser.thread_id is not None,
                                'uia_running': runtime.observations.uia.running,
                                'transient_uia_running': runtime._transient_worker.running},
                    'input_busy': runtime._input_lock.locked(),
                    'operations': runtime.operations.journal.active(owner)}
        cancelled = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(runtime.operations.status, owner, operation_id,
                                                    request_key=request_key, follow_ms=payload.get('follow_ms', 0),
                                                    cancelled=cancelled))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.05)
                if await request.is_disconnected():
                    cancelled.set()
                    break
            return await task
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except RuntimeFault as error:
            return {'success': False, 'error': {'code': error.code, 'message': str(error)}}
        except Cancelled as error:
            raise HTTPException(499, str(error)) from error
        finally:
            if not task.done():
                cancelled.set()

    @router.post("/companion/start")
    def start_companion(req: CompanionStartRequest):
        """Attach DSH-owned companion UI, including after a resident-kernel restart."""
        from . import companion

        companion.install(runtime)
        companion.get_companion().watch_client(req.client_pid)
        return {"success": True}

    @router.post("/companion/shutdown")
    def shutdown_companion():
        """DSH closed: hide its UI without killing a deliberately resident kernel."""
        from . import companion

        companion.shutdown()
        return {"success": True}

    @router.get("/health")
    def health():
        # `bridge.pid` 必须是**内核自己的** pid：
        # 插件在 elevated 启动路径下拿不到 child.pid（计划任务里进程不是它 spawn 的），
        # 只能从 health 里取。之前没有这个字段，导致 /health 返回 pid=null，
        # plugin 的 attach() 记录下来也是 null，排查时无法直接定位进程。
        # 实测 2026-10-27：`desktop_health` 返回 "pid": null。
        return {"status": "ok", "protocol": 2, "scheduler": "dsh", "parser": runtime.config.parser.engine,
                "execution_enabled": True,
                "delegated_tasks": False,
                "build": _BUILD, "epoch": runtime.epoch,
                "capabilities": ["runtime", "windows", "capture", "uia", "web", "lazy_vision", "unified_runtime"],
                "initialized_backends": runtime.initialized_backends(),
                "parser_initializations": runtime.parser_initializations,
                  "bridge": {"pid": os.getpid(), "source_root": str(_SOURCE_ROOT),
                             "input_mode": "physical", "decoration": "task_commander",
                             "input_revision": _INPUT_REVISION},
                  "cursor": ({"visible": desktop_tools._CURSOR.visible,
                              "target": desktop_tools._CURSOR._target,
                              "action": desktop_tools._CURSOR._resolved_action(time.monotonic()),
                              "guard_hides": getattr(desktop_tools._CURSOR, "guard_hides", 0),
                              "error": desktop_tools._CURSOR.error}
                             if desktop_tools._CURSOR is not None else {"visible": False, "started": False}),
                }

    @router.get("/scope")
    def scope_status():
        """Authorized applications/windows, plus legacy single-selection fields."""
        selections = desktop_tools.scope_selections()
        first = selections[0] if selections else None
        return {"locked": bool(selections), "window_id": first.get("window_id") if first else None,
                "window": first, "selections": selections,
                "scope": "applications", "stopped": input_gate.stop_engaged()}

    @router.post("/scope/resume")
    def resume_after_stop():
        input_gate.clear_stop()
        return {"success": True, "stopped": False}

    @router.post("/cursor/state")
    @router.post("/mascot/state")
    def cursor_state(req: CursorStateRequest, owner: str = Depends(authorize)):
        overlay = desktop_tools._cursor_overlay()
        if req.active:
            overlay.begin_task(owner, lease_seconds=35, client_pid=req.client_pid)
            if req.action: overlay.action(req.action)
        else:
            overlay.end_task(owner)
        return {"success": overlay.error is None, "active": req.active, "error": overlay.error}

    return router
