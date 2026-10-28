"""Capability ledger: what actually works, per app and per action.

Every action that goes through the bridge records its `effect`, so the matrix is
built from real usage instead of a synthetic probe that might click something the
user cares about. Entries are keyed by executable (plus a web bucket for the
controlled browser) and rendered as a markdown table for docs/action-support.md.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import dsh_desktop_home

_LOCK = threading.Lock()
_FILENAME = "ledger.json"
_EFFECTS = ("confirmed", "suspected_noop", "unverifiable", "refused", "error")


def _path() -> Path:
    root = dsh_desktop_home()
    root.mkdir(parents=True, exist_ok=True)
    return root / _FILENAME


def _load() -> dict:
    path = _path()
    if not path.is_file():
        return {"apps": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a corrupt ledger must not break actions
        return {"apps": {}}
    if not isinstance(data, dict) or not isinstance(data.get("apps"), dict):
        return {"apps": {}}
    return data


def record(exe: str, action: str, effect: str, *, title: str = "", detail: str = "") -> None:
    """Count one observed action outcome. Never raises."""
    exe = (exe or "unknown").strip().lower() or "unknown"
    action = (action or "unknown").strip()
    effect = effect if effect in _EFFECTS else "unverifiable"
    try:
        with _LOCK:
            data = _load()
            app = data["apps"].setdefault(exe, {"title": title or exe, "actions": {}})
            if title:
                app["title"] = title
            entry = app["actions"].setdefault(action, {key: 0 for key in _EFFECTS})
            entry[effect] = int(entry.get(effect, 0)) + 1
            entry["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            if detail:
                entry["last_detail"] = detail[:200]
            _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - telemetry must never block the action
        pass


def snapshot() -> dict:
    with _LOCK:
        return _load()


def summary(limit: int = 5) -> list[dict]:
    """Compact rows for the health tool: the most exercised actions."""
    rows: list[dict] = []
    for exe, app in snapshot().get("apps", {}).items():
        for action, entry in (app.get("actions") or {}).items():
            total = sum(int(entry.get(key, 0)) for key in _EFFECTS)
            rows.append({
                "exe": exe,
                "action": action,
                "confirmed": int(entry.get("confirmed", 0)),
                "noop": int(entry.get("suspected_noop", 0)),
                "refused": int(entry.get("refused", 0)),
                "total": total,
            })
    rows.sort(key=lambda row: -row["total"])
    return rows[: max(1, limit)]


def render_markdown() -> str:
    data = snapshot()
    apps = data.get("apps", {})
    lines = ["# 动作能力账本", "",
             "> 由真实使用累积：每次动作都会记下 `effect`，不是探测脚本猜的。",
             "> `confirmed` = 界面确有变化，`noop` = 没观察到变化，`refused` = 主动拒绝（如 capture_id 过期）。",
             ""]
    if not apps:
        lines.append("（还没有记录：跑几个动作后回来再看）")
        return "\n".join(lines) + "\n"
    lines += ["| 应用 | 动作 | confirmed | noop | refused | 合计 | 最近 |",
              "|---|---|---:|---:|---:|---:|---|"]
    for exe, app in sorted(apps.items()):
        for action, entry in sorted((app.get("actions") or {}).items()):
            total = sum(int(entry.get(key, 0)) for key in _EFFECTS)
            lines.append(
                f"| {app.get('title') or exe} (`{exe}`) | {action} | "
                f"{int(entry.get('confirmed', 0))} | {int(entry.get('suspected_noop', 0))} | "
                f"{int(entry.get('refused', 0))} | {total} | {entry.get('last_seen', '')} |"
            )
    return "\n".join(lines) + "\n"
