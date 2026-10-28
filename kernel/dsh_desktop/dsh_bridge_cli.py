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
    home = args.home_dir.resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["DSH_DESKTOP_HOME"] = os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_BUNDLED_SKILLS"] = str(Path(__file__).parent / "skills")
    os.environ["DSH_DESKTOP_PARSER_ENGINE"] = "local-deepseek"

    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from .config import Config
    from .dsh_bridge import create_router
    from .eternity import Eternity
    from .ui_rules import seed_knowledge
    from .weights import ensure_weights_ready

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config_path = home / "config.yaml"
    cfg = Config.from_yaml(str(config_path)) if config_path.is_file() else Config()
    cfg.parser.engine = "local-deepseek"
    cfg.cron.enabled = False
    cfg.tools.allow_shell = False
    cfg.model.provider = "deepseek"
    cfg.model.base_url = "https://api.deepseek.com"
    cfg.model.api_key = os.getenv("DEEPSEEK_API_KEY", "")
    cfg.model.default = cfg.model.fast = "deepseek-flash"
    ensure_weights_ready(Path(cfg.workspace.weights_dir))
    seeded = seed_knowledge(home)
    if seeded:
        logging.info("seeded %d UI rules into the knowledge index", seeded)
    eternity = Eternity(cfg)
    eternity.setup()

    @asynccontextmanager
    async def lifespan(app):
        yield
        eternity.shutdown(timeout=5)

    app = FastAPI(title="Desktop DSH task bridge", lifespan=lifespan)
    app.include_router(create_router(eternity))

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host") not in {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}:
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

    logging.info("DSH bridge ready: http://127.0.0.1:%d; Desktop cron disabled", args.port)
    if not cfg.model.api_key:
        logging.warning("DEEPSEEK_API_KEY is not set; task execution will be rejected")
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
