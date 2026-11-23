"""Run the existing Windows kernel with only the authenticated DSH API."""
from __future__ import annotations

import argparse
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="dsh_desktop-dsh")
    parser.add_argument("--home-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Desktop execution currently supports Windows only")
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    if len(os.getenv("DSH_DESKTOP_BRIDGE_TOKEN", "")) < 32:
        parser.error("Set DSH_DESKTOP_BRIDGE_TOKEN to a random secret of at least 32 characters")
    import win32api
    import win32event
    mutex = win32event.CreateMutex(None, False, "Global\\DesktopCore_Single_Instance")
    if win32api.GetLastError() == 183:
        win32api.CloseHandle(mutex)
        parser.error("Desktop is already running; close it before starting the DSH kernel")
    import atexit
    atexit.register(win32api.CloseHandle, mutex)
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(args.home_dir, args.port)
    logging.info("DSH desktop runtime ready on loopback port %d", args.port)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


def create_app(home: Path, port: int):
    home = home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["DSH_DESKTOP_HOME"] = str(home)

    # Must run before the first window/UIA call: without it Windows virtualises
    # coordinates by the system DPI and every click on a scaled display drifts.
    from .dpi import set_per_monitor_v2

    set_per_monitor_v2()

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from .dsh_bridge import create_router
    from .runtime import DesktopRuntime, RuntimeConfig

    cfg = RuntimeConfig.from_home(home)
    os.environ["DSH_DESKTOP_PARSER_ENGINE"] = cfg.parser.engine
    runtime = DesktopRuntime(cfg, home=home)
    runtime.setup()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            runtime.shutdown(timeout=5)

    app = FastAPI(title="Desktop DSH task bridge", lifespan=lifespan)
    app.include_router(create_router(runtime))

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return JSONResponse({"detail": "Invalid loopback host"}, status_code=403)
        if request.headers.get("origin"):
            return JSONResponse({"detail": "Cross-origin access denied"}, status_code=403)
        if request.method == "POST":
            length = request.headers.get("content-length", "")
            if not length.isdigit() or request.headers.get("transfer-encoding"):
                return JSONResponse({"detail": "A bounded Content-Length is required"}, status_code=411)
            if int(length) > 65_536:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
        return await call_next(request)

    app.state.runtime = runtime
    return app


if __name__ == "__main__":
    main()
