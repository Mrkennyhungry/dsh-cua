# AGENTS.md — dsh-cua (dsh-doraemon)

Windows computer-use plugin for **DeepSeek Harness (DSH)**. This file is the entry point for
any coding agent (Codex, Claude Code, or a DSH agent) working in this repository. Read it
before editing. It describes the framework contracts this package must satisfy.

**Development process is separate and mandatory:** branches, commit format, phase gates,
test baseline and versioning are defined in [`DEVELOPMENT.md`](./DEVELOPMENT.md). Read it
before your first commit; a change that violates it is not done, even if the code works.

---

## 1. What this package is

A DSH **Host plugin** (Cordis-based) that exposes desktop-automation tools to a DSH agent,
plus an optional Client (Web UI) module. The actual automation runs in a bundled **Python
kernel**; the plugin is the bridge between DSH's tool service and that kernel.

```
DSH agent
   │  calls tools (doraemon_*)
   ▼
index.js            Host plugin: registers tools, owns lifecycle
   │  HTTP/SSE
   ▼
bridge-client.js    typed client for the kernel bridge
   │
   ▼
kernel/*.py         Python kernel: UIA, OCR/YOLO, windows, browser, tasks
   │
   ▼
companion/*.cs      optional WinForms overlay (docked commander)
```

`client.js` is a separate entry (Web UI slots); it does not run in the Host process.

## 2. Required file layout

| File | Role | Must keep |
|---|---|---|
| `index.js` | Host entry, `main` in package.json | yes |
| `client.js` | Client entry, exported as `./client` | yes |
| `cordis.patch.yml` | Bundle patch that inserts the plugin row | yes |
| `bridge-client.js` | Kernel HTTP/SSE client | yes |
| `companion-state.js`, `cursor-state.js` | Host-side UI state mirrors | yes |
| `kernel/` | Python kernel | yes |
| `companion/` | Optional C# overlay sources | yes |
| `test/` | `node --test` suites | yes |

## 3. Host plugin contract (do not deviate)

`index.js` must export exactly these:

```js
export const name = 'doraemon'                 // plugin id
export const inject = ['tools', 'settings']    // services this plugin needs
export const Config = Schema.object({ ... })   // schemastery schema, not a plain object
export function apply(ctx, config) { ... }     // synchronous registration
```

Rules:

- **`Config` uses `@deepseek-ai/schemastery`**, not JSON Schema. Use `Schema.string()`,
  `Schema.number()`, `Schema.boolean()`, `.default(...)`. A field that must not restart the
  plugin on change is declared `.volatile()` and read through `config.field.get()`.
- **Register everything inside `apply`.** Do not start timers or background loops at module
  top level.
- **Release resources with `ctx.effect(() => async () => { ... })`** — the returned function
  runs on unload. The existing code stops running tasks and shuts the kernel down this way.
  Never rely on `process.on('exit')` for cleanup.
- **Do not read the profile's `cordis.patch.yml`.** It belongs to the user. To persist a
  setting, use the config editor:
  ```js
  const editor = ctx.get('configEditor')
  await editor.edit(ctx.fiber.entry, current => ({ ...current, field: value }))
  ```
- **Secrets never go into config.** Reference them by env name (`tokenEnv`,
  `apiKeyEnv`) or store them via `ctx.credentials`. `tokenEnv` is the established pattern here.

### 3.1 Tool definitions

```js
register(defineTool({
  name: 'doraemon_capture',
  description: '...',                          // shown verbatim to the model
  parameters: {
    scope: { type: 'string', enum: ['window', 'desktop'] },
    window_id: { type: 'string', required: true },
  },
  output: {
    schema: { type: 'object', properties: {}, additionalProperties: true },
    render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
  },
  async execute(args, exec) {
    // exec.callId      stable id for this call
    // exec.signal      AbortSignal — pass it into every fetch
    // exec.agent       { session } when available
    return { success: true, ... }
  },
}))
```

- `parameters` is a **plain object spec**, not JSON Schema: `{ type, required, description, enum }`.
  Required parameters use `required: true`.
- The shared `output` object at the top of `index.js` is the house style; reuse it unless a
  tool needs a custom renderer.
- **Throw** for invalid arguments; **return** `{ success: false, error }` for operational failures.
  `toolOk()` in `index.js` treats `success: false`, an `error` field, and `effect: 'refused'`
  as failure — keep those semantics.
- Every bridge call must forward `exec.signal`, otherwise a cancelled task leaks.

### 3.2 Bundle patch

`cordis.patch.yml` inserts exactly one row:

```yaml
- insert:
    - id: doraemon
      name: dsh-doraemon
      config: { ... }
```

Installed profiles may override `config` under the same `id`. Do not rename the `id` or the
package `name` — profiles target them by id.

### 3.3 Client entry

`package.json` declares it under `dsh.client`:

```json
"dsh": {
  "bundle": { "patch": "./cordis.patch.yml" },
  "client": { "platform": "web", "immediately": true,
              "inject": ["@deepseek-ai/dsh-client-ui-conversation",
                         "@deepseek-ai/dsh-client-ui-settings"] }
}
```

`client.js` runs in the browser bundle. It must not import Node built-ins or the kernel client.

## 4. Packaging rules

- `"type": "module"`, `"main": "index.js"`, `"os": ["win32"]`, `"engines": { "node": ">=22" }`.
- `@deepseek-ai/cordis`, `@deepseek-ai/dsh-tools`, `@deepseek-ai/schemastery` are
  **`peerDependencies` and optional** — DSH ships them. Never add them as `dependencies`, and
  never bundle copies.
- **`files[]` is a whitelist.** Any new runtime file (e.g. a new `kernel/*.py`) must be added
  there, plus any new top-level `.js`, or it will not be published.
- Bump `version` for every behavioural change; the profile pins a semver range.

## 5. Install and test

```powershell
# install or reinstall this directory into the running DSH profile
#   (DSH: Plugins > Add plugin, or plugin_manager install_bundle with the absolute path)

# node tests
npm test            # == node --test test/*.test.js

# python kernel tests
python -m pytest test -q
```

**Never** hand-write `~/.dsh/profiles/<name>/cordis.patch.yml`, the profile's `package.json`,
or anything under `$DSH_PROFILE_DIR/node_modules`. `install_bundle` owns those files. Never run
pnpm inside the profile directory.

After installing, verify in a **new** DSH session: the tool list must show the `doraemon_*`
tools, and `doraemon_status` must answer. Installation alone is not proof the tools work.

## 6. Repository conventions

- Comments explain **why**, not what. The existing code is dense with rationale — match it.
- Host code is ESM, no TypeScript, 2-space indent, single quotes, no semicolons.
- Public tool `description` strings are prompt text: keep them precise, and state what the tool
  does **not** do (existing tools do this, e.g. "Does not enable run_command").
- `private-knowledge/` is optional and git-ignored for the public build. Code that touches it
  must degrade silently when the directory is absent — see the `existsSync` guard in `apply`.
- Do not add a dependency without checking it is not already provided by the DSH installation.

## 7. Known constraints

- Windows only. UIA paths assume `comtypes`/`pywin32`; OCR/YOLO weights live under `vendor/weights`.
- The kernel requires `scripts/bootstrap.ps1` to have been run at least once.
- The plugin must never assume it can write into its own source directory — use `stateDir`
  (defaults to `~/.dsh/desktop`).
- Tool calls can run for minutes. Any new long-running operation needs a `follow`/`status`
  pair rather than a blocking call.
