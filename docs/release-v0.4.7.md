# dsh-doraemon v0.4.7

> [!WARNING]
> **Do not install this release. It ships a telemetry uploader that is on by default
> on the DSH plugin path.** `kernel/dsh_desktop/telemetry.py` posts usage metadata
> (persistent install id, version, tool names, error digests) to a third party, and
> the `telemetry_enabled: false` setting never reaches the plugin's entry point.
> Fixed by default-off in `b648cd3` and removed entirely in `b3a8c27`; the first
> clean release is **v0.4.8**. See
> [data boundary](data-boundary.md#五历史版本045047内置过遥测).

Package clean-up and task-feedback release. This is the first market version to carry
the docked commander, which landed on `main` after 0.4.6.

## Install

```
dsh-doraemon@git+https://github.com/Mrkennyhungry/dsh-cua.git#v0.4.7
```

Or add the index `https://raw.githubusercontent.com/Mrkennyhungry/dsh-cua/main/market/index.json`
on **Plugins → Discover** — it now defaults to 0.4.7. Steps 2–4 of
[`QUICKSTART.md`](https://github.com/Mrkennyhungry/dsh-cua/blob/main/QUICKSTART.md)
(bootstrap the Python runtime, restart, verify) are unchanged.

## Commander replaces the cosmetic cursor

- A docked, click-through pixel commander shows task state. It only reflects the task:
  its pose never decides whether an input succeeded.
- Where Windows supports it, the commander is excluded from screen capture. Its window
  class is also skipped by window enumeration, so it does not become a bind target.
- Coordinate actions use the real mouse, and the pointer stays where the action ended.
- `right_click_screen` verifies that the pointer reached the target, and scrolling uses
  raw mouse wheel detents.

See [commander mode](https://github.com/Mrkennyhungry/dsh-cua/blob/main/docs/commander-mode.md).

## Breaking changes for existing scripts

- `doraemon_cursor` is no longer registered, and the old `/cursor` route reports
  `CURSOR_REMOVED`. `DSH_DESKTOP_INPUT_MODE=virtual` does not bring the old input back.
- The standalone Desktop UI no longer has its knowledge import button, and
  `POST /api/knowledge/import` is gone. Knowledge files already on disk are untouched.
- The optional remote parser (`engine=omni` or `qwen`) reads its address and token only
  from Settings or `PARSER_SERVICE_URL` / `PARSER_SERVICE_TOKEN`. It now follows the
  system proxy like any other request: add the host to `NO_PROXY` if it must bypass one.
  The default `local-deepseek` engine does not use this service.

## Knowledge records

- Masking now covers passwords, cookies, authorization headers, session ids and common
  key formats, as well as API keys and tokens. It matches field names and known formats,
  so a bare secret typed into a window can still appear in a record. Existing knowledge
  files are not rewritten.
- Every session review is written as a `draft_` file and needs approval before it can be
  recalled. No kind of session skips that review.

## Packaging and updates

- `package.json#files` no longer lists the optional private catalog path, so a checkout
  that holds one cannot publish it by accident.
- Tests now scan every published text file for private hosts, private names and local
  user paths, rather than only the five entry files.
- The update check looks at this repository's Releases and accepts only links to them.
- The kernel's version string now matches the package version.

## Known test failures

`npm test` reports 11 failures. They are outdated tests, not behaviour regressions: nine
do not expect the commander's `/mascot/state` request, one expects the removed
`doraemon_cursor` in its tool count, and one still expects the old UIA node budget of 120
as well as the request above.

## Requirements

Windows, Node.js >= 22. The market entry declares DSH `>=0.2.1-rc.2 <0.3.0`. A Git-spec
install declares no client range, so it depends on the plugin APIs your client provides.
