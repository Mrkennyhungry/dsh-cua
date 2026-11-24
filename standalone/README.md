# Historical source-only adapters

The default DSH package publishes the unified runtime and six tools. This directory retains the previous Host registration and optional dependency list for historical callers and data recovery.

The Python source boundary includes standalone_bridge.py, legacy_bridge.py, eternity.py, controller.py, server.py, __main__.py, agent_tools.py, hermes_tools.py, knowledge.py, decide.py, web_decide.py, web_tools.py, IM/WeCom modules, cron/ and static/. They are excluded from the package whitelist. Shared Win32/UIA/input helpers continue to live in desktop_tools.py, and shared browser launch settings and DOM collection have a single implementation in browser_support.py and dom.py.

Use docs/MIGRATION-0.5.md for previous entry points, original data directories, backup and recovery. Reproduce old workbench deployments from their pinned Git revision with a separate environment and copied data.
