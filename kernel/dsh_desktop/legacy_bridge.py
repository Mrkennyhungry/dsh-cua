"""Optional delegated-agent bridge, isolated from the DSH desktop runtime."""
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse


def install_legacy_routes(router, eternity, authorize):
    from .dsh_bridge import TaskRequest, SteerRequest
    lock = threading.RLock()
    tasks = {}
    requests = {}

    def owned(task_id: str, owner: str) -> dict:
        with lock:
            task = tasks.get(task_id)
            if task is None or task["owner"] != owner:
                raise HTTPException(404, "Task not found")
            return task

    def status(task: dict) -> dict:
        sid = task["session_id"]
        running = eternity.is_running(sid)
        raw = None if running else eternity.wait_for_session(sid, timeout=0)
        result = None
        if isinstance(raw, dict):
            from .eternity import _run_end_details
            result = {**_run_end_details(raw), "final_response": str(raw.get("final_response") or "")[:12_000]}
        messages = eternity.get_session_messages(sid, limit=10).get("messages", [])
        progress = [{"role": str(m.get("role") or ""), "content": str(m.get("content") or "")[:1200]}
                    for m in messages if isinstance(m, dict)]
        return {"task_id": task["task_id"], "session_id": sid,
                "status": "running" if running else "finished" if result is not None else "unknown",
                "result": result, "progress": progress, "created_at": task["created_at"]}

    @router.post("/tasks")
    def start(req: TaskRequest, owner: str = Depends(authorize)):
        digest = hashlib.sha256(req.task.encode()).hexdigest()
        with lock:
            existing = requests.get((owner, req.request_id))
            if existing:
                task = tasks[existing]
                if task["digest"] != digest:
                    raise HTTPException(409, "request_id already used for a different task")
                return {"task_id": existing, "session_id": task["session_id"], "reused": True}
            if not os.getenv("DEEPSEEK_API_KEY", "").strip():
                raise HTTPException(503, "Set DEEPSEEK_API_KEY before executing tasks")
            if len(tasks) >= 2000:
                raise HTTPException(429, "Bridge task registry is full; restart only after all tasks finish")
            if any(eternity.is_running(t["session_id"]) for t in tasks.values()):
                raise HTTPException(409, "Desktop is busy; wait for the active task")
            if any(s.get("is_running") for s in eternity.list_sessions(limit=100)):
                raise HTTPException(409, "Desktop already has an active session")
            task_id = uuid.uuid4().hex
            try:
                sid = eternity.create_session(task=req.task, source="dsh")
            except (ValueError, RuntimeError):
                raise HTTPException(409, "Execution kernel could not start the task") from None
            tasks[task_id] = {"task_id": task_id, "session_id": sid, "owner": owner,
                              "digest": digest, "created_at": time.time()}
            requests[(owner, req.request_id)] = task_id
            return {"task_id": task_id, "session_id": sid, "reused": False}

    @router.get("/tasks/{task_id}")
    def get_status(task_id: str, owner: str = Depends(authorize)):
        return status(owned(task_id, owner))

    @router.post("/tasks/{task_id}/stop")
    def stop(task_id: str, owner: str = Depends(authorize)):
        task = owned(task_id, owner)
        requested = eternity.stop_session(task["session_id"])
        return {"stop_requested": requested, **status(task)}

    @router.post("/tasks/{task_id}/steer")
    def steer(task_id: str, req: SteerRequest, owner: str = Depends(authorize)):
        task = owned(task_id, owner)
        if not eternity.is_running(task["session_id"]):
            raise HTTPException(409, "Task finished; steering must not restart desktop actions")
        if not eternity.steer_session(task["session_id"], req.message):
            raise HTTPException(409, "Task could not accept steering")
        return {"status": "steered"}

    @router.get("/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, owner: str = Depends(authorize)):
        task = owned(task_id, owner)

        async def generate():
            yield "data: " + json.dumps({"event": "bridge_status", "data": status(task)}, ensure_ascii=False) + "\n\n"
            async for event in eternity.get_session_stream(task["session_id"]):
                if await request.is_disconnected():
                    break
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({"event": "bridge_status", "data": status(task)}, ensure_ascii=False) + "\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @router.get("/tasks/{task_id}/image")
    def image(task_id: str, path: str, owner: str = Depends(authorize)):
        task = owned(task_id, owner)
        root = Path(eternity.config.workspace.screenshot_dir).resolve()
        image_path = Path(path).resolve()
        if not image_path.is_relative_to(root) or image_path.suffix.lower() not in {".png", ".jpeg", ".jpg"}:
            raise HTTPException(403, "Image outside screenshot directory")
        messages = eternity.get_session_messages(task["session_id"], limit=500).get("messages", [])
        refs = set()
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except (ValueError, TypeError):
                    content = None
            if not isinstance(content, dict):
                continue
            for key in ("image_path", "SoM_image_path"):
                value = content.get(key)
                if isinstance(value, str):
                    refs.add(str(Path(value).resolve()))
        if str(image_path) not in refs:
            raise HTTPException(403, "Image is not referenced by this task")
        if not image_path.is_file():
            raise HTTPException(404, "Image not found")
        return FileResponse(image_path)
