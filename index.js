import Schema from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createBridge, ownerOf } from './bridge-client.js'

export const name = 'doraemon'
export const inject = ['tools']
export const Config = Schema.object({
  port: Schema.number().default(8766),
  tokenEnv: Schema.string().default('DSH_DESKTOP_BRIDGE_TOKEN'),
  waitSeconds: Schema.number().default(600),
  // Start the local kernel on demand, hidden, instead of asking the user to pre-launch it.
  autoStart: Schema.boolean().default(true),
  // False keeps the bridge a plain HTTP client (tests, or a kernel managed elsewhere).
  manageKernel: Schema.boolean().default(true),
  bootSeconds: Schema.number().default(120),
  // Keep an auto-started kernel alive after DSH exits, so the next session reconnects instantly.
  keepAlive: Schema.boolean().default(true),
  repoRoot: Schema.string().default(''),
  pythonPath: Schema.string().default(''),
  homeDir: Schema.string().default(''),
  // Where kernel state, logs and the optional key file live. Defaults to
  // ~/.dsh/desktop so the plugin never needs write access to its source directory.
  stateDir: Schema.string().default(''),
  // Pre-P1 name for stateDir, still honoured for existing profiles.
  logDir: Schema.string().default(''),
  apiKeyFile: Schema.string().default(''),
  enableExperimentalPageCache: Schema.boolean().default(false),
})

const taskParameter = { type: 'string', required: true, description: 'The task_id returned by doraemon_run, in this DSH session.' }
const output = {
  schema: { type: 'object', properties: {}, additionalProperties: true },
  render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
}

const MAX_TIMELINE_STEPS = 200

function toolOk(result) {
  if (!result || typeof result !== 'object') return true
  if (result.success === false) return false
  if (result.error) return false
  // `refused` means the action never ran, so it must not look like a success.
  return result.effect !== 'refused'
}

function parseToolResult(raw) {
  if (raw && typeof raw === 'object') return raw
  if (typeof raw === 'string' && raw.length < 20000) {
    try {
      const value = JSON.parse(raw)
      return value && typeof value === 'object' ? value : null
    } catch { return null }
  }
  return null
}

/**
 * Task-level timing derived from the kernel's own `tool_call` / `tool_result`
 * events (dsh_desktop/events.py), so the kernel needs no extra instrumentation.
 *
 * Pairing rules:
 * - `tool_call` opens a span keyed by call_id; `tool_result` closes it.
 * - `duration_ms` comes from the kernel when present, otherwise from the arrival
 *   gap and is marked `estimated: true`.
 * - `timing_ms` inside a tool result (analyze: capture/parse/caption/save/annotate)
 *   is kept nested instead of being flattened into one number.
 * - Task span runs from the first `tool_call` to the terminal `bridge_status`.
 */
export function createToolTimeline(clock = () => Date.now()) {
  const open = new Map()
  const steps = []
  let firstCallAt = null
  let lastAt = null
  let finishedAt = null
  let anonymous = 0

  function push(step) {
    steps.push(step)
    if (steps.length > MAX_TIMELINE_STEPS) steps.shift()
  }

  return {
    observe(event) {
      const at = clock()
      lastAt = at
      const type = event?.event
      const data = event?.data
      if (!data || typeof data !== 'object') return

      if (type === 'tool_call') {
        if (firstCallAt === null) firstCallAt = at
        const key = typeof data.call_id === 'string' && data.call_id ? data.call_id : `anon-${++anonymous}`
        open.set(key, { name: typeof data.name === 'string' && data.name ? data.name : 'tool', at })
        return
      }

      if (type === 'tool_result') {
        const key = typeof data.call_id === 'string' && data.call_id ? data.call_id : null
        const started = key ? open.get(key) : null
        if (key) open.delete(key)
        const result = parseToolResult(data.result)
        const reported = typeof data.duration_ms === 'number' && Number.isFinite(data.duration_ms)
          ? Math.max(0, Math.round(data.duration_ms))
          : null
        const step = {
          name: (typeof data.name === 'string' && data.name) || started?.name || 'tool',
          duration_ms: reported ?? (started ? Math.max(0, at - started.at) : 0),
          ok: toolOk(result),
        }
        if (reported === null) step.estimated = true
        if (result?.timing_ms && typeof result.timing_ms === 'object') step.timing_ms = result.timing_ms
        if (typeof result?.effect === 'string') step.effect = result.effect
        push(step)
        return
      }

      if (type === 'bridge_status' && typeof data.status === 'string'
        && data.status !== 'running' && finishedAt === null) {
        finishedAt = at
      }
    },

    summary() {
      const totalToolMs = steps.reduce((sum, step) => sum + (step.duration_ms || 0), 0)
      const slowest = [...steps]
        .sort((a, b) => (b.duration_ms || 0) - (a.duration_ms || 0))
        .slice(0, 3)
        .map((step) => ({ name: step.name, duration_ms: step.duration_ms, ok: step.ok }))
      return {
        timeline: steps,
        tool_calls: steps.length,
        total_tool_ms: totalToolMs,
        slowest,
        task_ms: firstCallAt !== null && finishedAt !== null ? finishedAt - firstCallAt : null,
      }
    },
  }
}

/**
 * One default, overridable in two places: the plugin config, then DSH_DESKTOP_PORT.
 * The shipped cordis.patch.yml no longer pins a port, so a conflict is fixed by
 * an env var instead of editing a file that lives inside the package.
 */
function resolvePort(configured) {
  const raw = process.env.DSH_DESKTOP_PORT
  if (raw === undefined || raw === '') return configured
  const value = Number(raw)
  if (!Number.isInteger(value) || value < 1024 || value > 65535) {
    throw new Error(`DSH_DESKTOP_PORT must be an integer between 1024 and 65535 (got ${JSON.stringify(raw)})`)
  }
  return value
}

export function apply(ctx, config) {
  if (!Number.isInteger(config.waitSeconds) || config.waitSeconds < 1 || config.waitSeconds > 3600) {
    throw new Error('waitSeconds must be an integer between 1 and 3600')
  }
  if (!Number.isInteger(config.bootSeconds) || config.bootSeconds < 5 || config.bootSeconds > 900) {
    throw new Error('bootSeconds must be an integer between 5 and 900')
  }
  if (!/^[A-Z_][A-Z0-9_]*$/.test(config.tokenEnv)) throw new Error('Invalid token environment variable')
  config = { ...config, port: resolvePort(config.port) }
  // Reuse the key already configured in this DSH window instead of asking for a second copy.
  // Read lazily through ctx.get so a composition without the credentials service still loads.
  const bridge = createBridge(config, fetch, {
    resolveApiKey: async () => {
      const credentials = typeof ctx.get === 'function' ? ctx.get('credentials') : ctx.credentials
      if (!credentials || typeof credentials.resolve !== 'function') {
        throw new Error('credentials service is unavailable')
      }
      const hit = await credentials.resolve('DEEPSEEK_API_KEY')
      return typeof hit?.value === 'string' ? hit.value : undefined
    },
  })
  const active = new Map()
  const registered = []
  const register = (tool) => {
    ctx.tools.register(tool)
    registered.push(tool.name)
  }

  // The private product package carries this optional component catalog. The
  // public package omits the file, so it never registers a product-specific tool.
  const productCatalogPath = join(dirname(fileURLToPath(import.meta.url)), 'private-knowledge', 'console-components.json')
  if (existsSync(productCatalogPath)) {
    let productEntries = []
    try {
      const parsed = JSON.parse(readFileSync(productCatalogPath, 'utf8'))
      productEntries = Array.isArray(parsed?.entries) ? parsed.entries : []
    } catch (error) {
      console.warn('[doraemon] optional product component catalog could not be loaded: %s', error?.message ?? error)
    }
    if (productEntries.length) register(defineTool({
      name: 'doraemon_product_knowledge',
      description: 'Search the bundled product console component catalog for UI structure, component class signatures, route coverage and verification hints. Use before navigating unfamiliar console pages; this catalog describes components and does not authorize destructive actions.',
      parameters: {
        query: { type: 'string', required: true, description: 'Page, component, action or visible label to search for.' },
        limit: { type: 'number', description: 'Maximum matches, 1–10 (default 5).' },
      },
      output,
      execute(args) {
        const query = String(args.query ?? '').trim().toLowerCase()
        if (!query || query.length > 300) throw new Error('query must contain 1–300 characters')
        const limit = args.limit === undefined ? 5 : Number(args.limit)
        if (!Number.isInteger(limit) || limit < 1 || limit > 10) throw new Error('limit must be 1–10')
        const terms = query.split(/[\s,，、]+/).filter(Boolean)
        const ranked = productEntries.map((entry) => {
          const haystack = [entry.family, entry.description, ...(entry.aliases ?? []), ...(entry.routes ?? []), ...(entry.classes ?? [])]
            .join(' ').toLowerCase()
          return { entry, score: terms.reduce((score, term) => score + (haystack.includes(term) ? 1 : 0), 0) }
        }).filter((row) => row.score > 0).sort((a, b) => b.score - a.score)
        return { success: true, matches: ranked.slice(0, limit).map(({ entry, score }) => ({ ...entry, score })) }
      },
    }))
  }
  ctx.effect(() => async () => {
    if (bridge.ready) {
      await Promise.allSettled([...active].map(([taskId, owner]) =>
        bridge.request(bridge.taskPath(taskId, '/stop'), owner, { body: {} })))
    }
    active.clear()
    if (!config.keepAlive) await bridge.shutdown(true)
  })

  // The kernel streams token-level deltas, so a long task can emit thousands of
  // frames: keep a bounded buffer and only surface the meaningful ones.
  const NOISE_EVENTS = new Set(['reasoning', 'token', 'delta', 'chunk', 'content'])
  const MAX_EVENTS = 500

  function summarise(event) {
    const type = typeof event?.event === 'string' ? event.event : 'event'
    const data = event?.data
    if (type === 'bridge_status' && data?.status) return { type, status: data.status }
    if (data && typeof data === 'object') {
      const text = [data.message, data.text, data.content, data.summary, data.name]
        .find((value) => typeof value === 'string' && value.trim())
      return text ? { type, text: text.slice(0, 200) } : { type }
    }
    return { type }
  }

  function recentEvents(events) {
    return events.filter((event) => !NOISE_EVENTS.has(event?.event)).map(summarise).slice(-40)
  }

  /**
   * Follow a task over the bridge's SSE stream instead of polling.
   * Resolves when the task reports a terminal status or the window elapses.
   */
  async function follow(taskId, owner, exec, seconds) {
    const events = []
    const timeline = createToolTimeline()
    let total = 0
    let final = null
    let streamError = null
    const deadline = new AbortController()
    const timer = setTimeout(() => deadline.abort(new Error('follow window elapsed')), seconds * 1000)
    const signal = exec.signal ? AbortSignal.any([exec.signal, deadline.signal]) : deadline.signal
    try {
      await bridge.stream(bridge.taskPath(taskId, '/events'), owner, {
        signal,
        timeoutMs: (seconds + 10) * 1000,
        onEvent(event) {
          total += 1
          // The coarse ring buffer and the timeline are different shapes: keep the
          // per-tool spans separate so a noisy stream cannot evict them.
          timeline.observe(event)
          if (events.length >= MAX_EVENTS) events.shift()
          events.push(event)
          if (event?.event === 'bridge_status' && event?.data?.status
            && event.data.status !== 'running') final = event.data
        },
      })
    } catch (error) {
      const timedOut = deadline.signal.aborted && !exec.signal?.aborted
      if (!final && !timedOut) streamError = String(error?.message ?? error)
    } finally {
      clearTimeout(timer)
    }
    return { events, total, final, streamError, timing: timeline.summary() }
  }

  register(defineTool({
    name: 'doraemon_run',
    description: 'Execute an authorized Windows desktop/browser automation task using Doraemon. Reuses its existing UIA/DOM, local CPU OCR/YOLO, DeepSeek icon captions and agent sessions. Call from DSH scheduled tasks as the sole scheduler. Does not enable run_command. The local kernel is started hidden in the background on first use. This is not a sandbox: the trusted local kernel retains desktop-owner permissions and its existing Python tools. By default the call follows the task on the bridge event stream and returns as soon as it finishes; if the window elapses the task keeps running, so continue with doraemon_status/stop/steer. Never blindly retry an uncertain submission; reuse request_id to reconcile.',
    parameters: {
      task: { type: 'string', required: true, description: 'Exact authorized task, targets and verification requirements.' },
      request_id: { type: 'string', description: 'Stable idempotency key for this scheduled occurrence, reused only when reconciling the same task.' },
      follow: { type: 'boolean', description: 'Stream events until the task finishes (default true). false returns immediately with a task id for polling.' },
      follow_seconds: { type: 'number', description: 'How long to follow, 5–3600 (default: the configured wait window).' },
    },
    output,
    async execute(args, exec) {
      const owner = ownerOf(exec)
      if (!args.task.trim() || args.task.length > 20000) throw new Error('Task must contain 1–20000 characters')
      const requestId = args.request_id ?? String(exec.callId)
      if (!/^[a-zA-Z0-9_.:-]{1,160}$/.test(requestId)) throw new Error('Invalid request_id')
      const started = await bridge.request('/tasks', owner, { body: { task: args.task, request_id: requestId }, signal: exec.signal })
      if (!started || !/^[a-f0-9]{32}$/.test(started.task_id)) throw new Error('Invalid task handle from bridge')
      active.set(started.task_id, owner)
      try {
        if (args.follow === false) {
          const value = await bridge.wait(started.task_id, owner, exec.signal, config.waitSeconds)
          if (value.status !== 'running') active.delete(started.task_id)
          return { ...value, request_id: requestId, reused: Boolean(started.reused) }
        }
        const window = args.follow_seconds === undefined
          ? Math.min(config.waitSeconds || 300, 300)
          : Math.max(5, Math.min(3600, Number(args.follow_seconds)))
        const { events, total, final, streamError, timing } = await follow(started.task_id, owner, exec, window)
        if (final) {
          active.delete(started.task_id)
          return { ...final, request_id: requestId, reused: Boolean(started.reused),
            events: total, recent_events: recentEvents(events), ...timing }
        }
        const value = await bridge.request(bridge.taskPath(started.task_id), owner, { signal: exec.signal })
        if (value.status !== 'running') active.delete(started.task_id)
        return { ...value, request_id: requestId, reused: Boolean(started.reused), events: total,
          recent_events: recentEvents(events), ...timing,
          note: streamError
            ? `event stream unavailable (${streamError}); fell back to status polling`
            : 'follow window elapsed; the task is still running' }
      } catch (error) {
        if (exec.signal.aborted) {
          await bridge.request(bridge.taskPath(started.task_id, '/stop'), owner, { body: {} }).catch(() => {})
        }
        throw error
      }
    },
  }))

  register(defineTool({
    name: 'doraemon_status', description: 'Read actual task status and recent progress. finished is execution end, not proof of task success; inspect result. With wait_seconds > 0 it follows the event stream until the task finishes or the window elapses.',
    parameters: {
      task_id: taskParameter,
      wait_seconds: { type: 'number', description: 'Follow the event stream for up to this long, 0–600 (default 0: read the current status once).' },
    },
    output,
    async execute(args, exec) {
      const owner = ownerOf(exec)
      const waitSeconds = args.wait_seconds === undefined ? 0 : Math.max(0, Math.min(600, Number(args.wait_seconds)))
      if (!Number.isFinite(waitSeconds)) throw new Error('wait_seconds must be a number')
      if (waitSeconds > 0) {
        const { events, total, final, streamError, timing } = await follow(args.task_id, owner, exec, waitSeconds)
        if (final) {
          active.delete(args.task_id)
          return { ...final, events: total, recent_events: recentEvents(events), ...timing }
        }
        const value = await bridge.request(bridge.taskPath(args.task_id), owner, { signal: exec.signal })
        return { ...value, events: total, recent_events: recentEvents(events), ...timing,
          ...(streamError ? { note: `event stream unavailable (${streamError}); read status directly` } : {}) }
      }
      const value = await bridge.request(bridge.taskPath(args.task_id), owner, { signal: exec.signal })
      if (value.status !== 'running') active.delete(args.task_id)
      return value
    },
  }))

  register(defineTool({
    name: 'doraemon_stop', description: 'Request cancellation of this session’s Doraemon task. stop_requested is not proof that execution has stopped.',
    parameters: { task_id: taskParameter }, output,
    async execute(args, exec) {
      return bridge.request(bridge.taskPath(args.task_id, '/stop'), ownerOf(exec), { body: {}, signal: exec.signal })
    },
  }))

  register(defineTool({
    name: 'doraemon_steer', description: 'Inject a correction or takeover instruction into a running task without replacing its conversation history. Finished tasks cannot be resumed with this tool.',
    parameters: { task_id: taskParameter, message: { type: 'string', required: true, description: 'Correction for the current task.' } }, output,
    async execute(args, exec) {
      if (!args.message.trim() || args.message.length > 10000) throw new Error('Invalid steering message')
      return bridge.request(bridge.taskPath(args.task_id, '/steer'), ownerOf(exec), { body: { message: args.message }, signal: exec.signal })
    },
  }))

  const windowIdParameter = { type: 'string', required: true, description: 'Bound window id returned by doraemon_windows (action=bind).' }
  const captureParameter = {
    type: 'string',
    description: 'capture_id from the latest doraemon_analyze. A stale id makes the action refuse instead of clicking the wrong place.',
  }

  // Actions report effect: confirmed | suspected_noop | unverifiable | refused,
  // plus an escalation hint when the change could not be observed.
  const pinCapture = (value) => (typeof value === 'string' && value ? { capture_id: value } : {})
  // A click may spawn its window in another process, seconds later; 0 means "do not wait".
  const windowWait = (args) => {
    const value = Number(args.window_wait_ms)
    return Number.isFinite(value) && value >= 0 ? { window_wait_ms: Math.round(value) } : {}
  }

  function boundWindowId(value) {
    const id = String(value ?? '')
    if (!/^win-[a-f0-9]{10}$/.test(id)) throw new Error('Invalid window_id; bind a window first with doraemon_windows')
    return id
  }

  function unit(value, name) {
    const number = Number(value)
    if (!Number.isInteger(number) || number < 0 || number > 1000) {
      throw new Error(`${name} must be an integer in 0–1000 (normalized window coordinates)`)
    }
    return number
  }

  register(defineTool({
    name: 'doraemon_windows',
    description: 'List, bind, activate and unbind desktop windows through the local Doraemon kernel. Bind first (action=bind with an hwnd from action=list, or action=foreground for the current foreground window), then pass the returned window_id to doraemon_analyze / doraemon_uia / doraemon_click / doraemon_type / doraemon_key / doraemon_scroll / doraemon_swipe.',
    parameters: {
      action: { type: 'string', required: true, description: 'list | bound | bind | foreground | activate | unbind' },
      hwnd: { type: 'number', description: 'Native window handle for action=bind, as returned by action=list.' },
      label: { type: 'string', description: 'Title or process substring for action=bind or foreground.' },
      window_id: { type: 'string', description: 'Bound window id for action=activate or unbind.' },
      wait: { type: 'number', description: 'Seconds to wait after activation, 0–10 (default 1).' },
    },
    output,
    async execute(args, exec) {
      const owner = ownerOf(exec)
      const action = String(args.action ?? '').toLowerCase()
      if (action === 'list') return bridge.request('/windows', owner, { signal: exec.signal })
      if (action === 'bound') return bridge.request('/windows/bound', owner, { signal: exec.signal })
      if (action === 'bind') {
        const hwnd = Number.isInteger(args.hwnd) ? args.hwnd : null
        return bridge.request('/windows/bind', owner, {
          body: { hwnd, label: String(args.label ?? '') }, signal: exec.signal,
        })
      }
      if (action === 'foreground') {
        return bridge.request('/windows/foreground', owner, {
          body: { label: String(args.label ?? '') }, signal: exec.signal,
        })
      }
      const id = boundWindowId(args.window_id)
      if (action === 'unbind') return bridge.request(`/windows/${id}/unbind`, owner, { body: {}, signal: exec.signal })
      if (action === 'activate') {
        const wait = args.wait === undefined ? 1 : Number(args.wait)
        if (!Number.isFinite(wait) || wait < 0 || wait > 10) throw new Error('wait must be between 0 and 10 seconds')
        return bridge.request(`/windows/${id}/activate`, owner, { body: { wait }, signal: exec.signal })
      }
      throw new Error('Unknown action: use list, bound, bind, foreground, activate or unbind')
    },
  }))

  register(defineTool({
    name: 'doraemon_analyze',
    description: 'Capture and parse UI elements into boxes normalized to 0–1000. The default local-deepseek engine runs YOLO icon detection plus RapidOCR on this machine and adds DeepSeek captions for unlabelled icons — no GPU service, no focus change, roughly 2.5s. scope="window" (default) captures only the bound window client area and is the most precise. scope="desktop" captures the whole screen: use it when a dropdown/flyout/menu lives in its own top-level window and therefore falls outside the client rect — those elements come back with screen_rect (absolute pixels) which you click via doraemon_click_screen. Returns ui_elements (bbox, text, caption, type), space, timing_ms and the annotated screenshot path.',
    parameters: {
      window_id: windowIdParameter,
      engine: { type: 'string', description: 'omni | qwen | local | local-deepseek; omit for the kernel default (normally local-deepseek).' },
      caption: { type: 'boolean', description: 'Generate icon captions on the local-deepseek engine (default true).' },
      annotate: { type: 'boolean', description: 'Write an annotated preview image next to the screenshot (default true).' },
      scope: { type: 'string', enum: ['window', 'desktop'], description: 'window = bound window client area (precise, default). desktop = whole screen including popups/overlays; coordinates then live in screen pixels.' },
      region: { type: 'string', enum: ['auto', 'window', 'full'], description: 'scope=desktop only: how much of the desktop to parse. auto (default) crops to the target window plus popups overlapping it — so OCR does not read unrelated windows; window = target window only; full = entire desktop.' },
      recover_black: { type: 'boolean', description: 'When the capture is near-black (minimized window), allow one activating retry. Off by default because activating can collapse a CEF dropdown.' },
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const scope = args.scope === 'desktop' ? 'desktop' : 'window'
      const region = ['auto', 'window', 'full'].includes(args.region) ? args.region : 'auto'
      return bridge.request(`/windows/${id}/analyze`, ownerOf(exec), {
        body: {
          engine: String(args.engine ?? ''), caption: args.caption !== false,
          annotate: args.annotate !== false, scope, region,
          recover_black: args.recover_black === true,
        },
        signal: exec.signal, timeoutMs: 60_000,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_uia',
    description: 'First-line observation for native desktop controls: read UIA names, values and rectangles without a screenshot. Set target_hint to the control/message you need; the kernel progressively checks UIA and, only when the target is missing or the message surface is sparse, falls back to local OCR/OmniParser in the same call. target_kind=visual requests OCR directly. This returns parsed text/boxes, not an image.',
    parameters: {
      window_id: windowIdParameter,
      max_depth: { type: 'number', description: 'Tree depth limit, 1–20 (default 7).' },
      max_nodes: { type: 'number', description: 'Node budget, 10–500 (default 120).' },
      target_hint: { type: 'string', description: 'Optional target label or text to look for. With a target, UIA expands progressively and falls back to OCR if the target is absent.' },
      target_kind: { type: 'string', description: 'control (default), message (message surface may be OCR-only), or visual (run local OCR directly).' },
      max_attempts: { type: 'number', description: 'UIA expansion attempts when target_hint is set, 1–3 (default 3).' },
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const maxDepth = args.max_depth === undefined ? 7 : Number(args.max_depth)
      const maxNodes = args.max_nodes === undefined ? 120 : Number(args.max_nodes)
      const targetHint = String(args.target_hint ?? '').trim()
      const targetKind = String(args.target_kind ?? 'control').toLowerCase()
      const maxAttempts = args.max_attempts === undefined ? 3 : Number(args.max_attempts)
      if (!Number.isInteger(maxDepth) || maxDepth < 1 || maxDepth > 20) throw new Error('max_depth must be 1–20')
      if (!Number.isInteger(maxNodes) || maxNodes < 10 || maxNodes > 500) throw new Error('max_nodes must be 10–500')
      if (!['control', 'message', 'visual'].includes(targetKind)) throw new Error('target_kind must be control, message or visual')
      if (!Number.isInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 3) throw new Error('max_attempts must be 1–3')
      return bridge.request(`/windows/${id}/uia`, ownerOf(exec), {
        body: {
          max_depth: maxDepth, max_nodes: maxNodes,
          target_hint: targetHint, target_kind: targetKind, max_attempts: maxAttempts,
        },
        signal: exec.signal, timeoutMs: 60_000,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_uia_batch',
    description: 'Batch a few safe actions in the SAME unchanged window after one UIA scan, so they do not require a model round trip after every step. Use only when the target controls and layout are already known and these actions will not navigate, open dialogs, submit, send, delete or otherwise change the page structure. Supports click/type/wait; stops at the first failure and never retries. Re-observe after any layout/state transition.',
    parameters: {
      window_id: windowIdParameter,
      steps: {
        type: 'array', required: true,
        description: 'Ordered steps (1–20). Each is {action:"click", name:"..."} or {action:"type", name:"...", text:"..."} or {action:"wait", ms:250}. Use an exact unique UIA name or auto_id for click/type.',
        items: {
          type: 'object',
          additionalProperties: false,
          properties: {
            action: { type: 'string', required: true, description: 'click, type or wait.' },
            name: { type: 'string', description: 'Exact unique UIA control name.' },
            auto_id: { type: 'string', description: 'Exact unique UIA automation id; preferred when available.' },
            text: { type: 'string', description: 'Text for action=type.' },
            ms: { type: 'number', description: 'Milliseconds for action=wait.' },
          },
        },
      },
      max_depth: { type: 'number', description: 'UIA scan depth, 1–10 (default 10).' },
      max_nodes: { type: 'number', description: 'UIA scan node budget, 20–1500 (default 600).' },
      timeout_s: { type: 'number', description: 'Whole batch timeout, greater than 0 and at most 120 seconds (default 30).' },
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const steps = args.steps
      if (!Array.isArray(steps) || steps.length < 1 || steps.length > 20) throw new Error('steps must contain 1–20 actions')
      for (const [i, step] of steps.entries()) {
        if (!step || typeof step !== 'object' || !['click', 'type', 'wait'].includes(String(step.action ?? ''))) {
          throw new Error(`steps[${i}].action must be click, type or wait`)
        }
        if (step.action !== 'wait' && !(String(step.name ?? '').trim() || String(step.auto_id ?? '').trim())) {
          throw new Error(`steps[${i}] requires an exact name or auto_id`)
        }
      }
      const maxDepth = args.max_depth === undefined ? 10 : Number(args.max_depth)
      const maxNodes = args.max_nodes === undefined ? 600 : Number(args.max_nodes)
      const timeout = args.timeout_s === undefined ? 30 : Number(args.timeout_s)
      if (!Number.isInteger(maxDepth) || maxDepth < 1 || maxDepth > 10) throw new Error('max_depth must be 1–10')
      if (!Number.isInteger(maxNodes) || maxNodes < 20 || maxNodes > 1500) throw new Error('max_nodes must be 20–1500')
      if (!Number.isFinite(timeout) || timeout <= 0 || timeout > 120) throw new Error('timeout_s must be >0 and <=120')
      return bridge.request(`/windows/${id}/uia-batch`, ownerOf(exec), {
        body: { steps, max_depth: maxDepth, max_nodes: maxNodes, timeout_s: timeout },
        signal: exec.signal, timeoutMs: Math.ceil(timeout * 1000) + 10_000,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_wait_window',
    description: 'Wait for a window that the last action on this window spawned. Apps often open their result in a separate process seconds later (a few seconds after the click, sometimes more), so the action reports no visible change while the real result is still loading. Returns the new windows (hwnd/title/exe) so you can bind and analyze them.',
    parameters: {
      window_id: windowIdParameter,
      timeout_ms: { type: 'number', description: 'How long to wait, 200–60000 (default 15000).' },
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const timeout = args.timeout_ms === undefined ? 15_000 : Number(args.timeout_ms)
      if (!Number.isFinite(timeout) || timeout < 200 || timeout > 60_000) throw new Error('timeout_ms must be 200–60000')
      return bridge.request(`/windows/${id}/wait-window`, ownerOf(exec), {
        body: { timeout_ms: Math.round(timeout) }, signal: exec.signal, timeoutMs: 70_000,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_click_screen',
    description: 'Click at absolute SCREEN pixels. Use this for popups that live outside the target window (dropdown menus, flyouts, native context menus): those are separate top-level windows, so window-normalized coordinates cannot reach them. Take the point from doraemon_analyze(scope="desktop") → ui_elements[].screen_rect centre. No window binding is required.',
    parameters: {
      x: { type: 'number', description: 'Absolute screen x in physical pixels (may be negative on a left-hand monitor).' },
      y: { type: 'number', description: 'Absolute screen y in physical pixels.' },
      clicks: { type: 'number', description: 'Click count, 1–3 (default 1).' },
    },
    output,
    async execute(args, exec) {
      const x = Number(args.x)
      const y = Number(args.y)
      if (!Number.isInteger(x) || !Number.isInteger(y)) throw new Error('x and y must be integers (screen pixels)')
      const clicks = args.clicks === undefined ? 1 : Number(args.clicks)
      if (!Number.isInteger(clicks) || clicks < 1 || clicks > 3) throw new Error('clicks must be 1–3')
      return bridge.request('/screen/click', ownerOf(exec), {
        body: { x, y, clicks }, signal: exec.signal, timeoutMs: 30_000,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_click',
    description: 'Click inside a bound window. Two ways to aim: normalized 0–1000 client coordinates (from a doraemon_analyze or doraemon_uia bbox centre), or goal="登录按钮" — the local decision layer picks the element from the window’s own element table and clicks it, so the table never enters your context. Prefer goal when you know the label but do not want to read the table. button=right opens a context menu.',
    parameters: {
      window_id: windowIdParameter,
      x: { type: 'number', description: 'Normalized x, 0–1000. Optional when goal is given.' },
      y: { type: 'number', description: 'Normalized y, 0–1000. Optional when goal is given.' },
      goal: { type: 'string', description: 'What to click, e.g. "登录按钮" or "点击「新建任务」". Resolved locally; nothing is clicked when the match is below the confidence gate.' },
      source: { type: 'string', description: 'Element table for goal: auto (default, UIA then OCR) | uia | ocr.' },
      caption: { type: 'boolean', description: 'Annotate icons so nameless buttons (close/minimise) become addressable by name. Costs one caption pass (~3s) and is cached.' },
      window_wait_ms: { type: 'number', description: 'How long to watch for a window this click spawns, 0–30000 (default 600). Many apps open their result in a separate process, seconds later.' },
      clicks: { type: 'number', description: 'Click count, 1–3 (default 1).' },
      button: { type: 'string', description: 'left (default) or right.' },
      capture_id: captureParameter,
      dispatch: { type: 'string', description: 'input (default, SendInput) | message (PostMessage, no mouse movement but Chromium often ignores it) | auto (UIA invoke by name when given, else SendInput).' },
      name: { type: 'string', description: 'Control name for dispatch=auto: when UIA can invoke it, the click is done semantically without moving the mouse.' },
      key: { type: 'string', description: 'Semantic key resolved from the knowledge base when name is missing (dispatch=auto).' },
      scope: { type: 'string', description: 'Scope for the key lookup, usually the app exe; empty means global.' },
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const owner = ownerOf(exec)
      const clicks = args.clicks === undefined ? 1 : Number(args.clicks)
      if (!Number.isInteger(clicks) || clicks < 1 || clicks > 3) throw new Error('clicks must be 1–3')
      const button = String(args.button ?? 'left').toLowerCase()
      if (button !== 'left' && button !== 'right') throw new Error('button must be left or right')

      const goal = String(args.goal ?? '').trim()
      if (goal) {
        const decided = await bridge.request(`/windows/${id}/decide`, owner, {
          body: { goal, source: String(args.source ?? 'auto'), caption: args.caption === true },
          signal: exec.signal, timeoutMs: 60_000,
        })
        if (!decided?.success || !decided.picked?.bbox) {
          return {
            success: false, effect: 'refused', goal,
            score: decided?.score ?? null, reason: decided?.reason ?? 'no decision',
            candidates: decided?.candidates ?? [],
            error: '本地决策层没有找到可信元素（分数低于门限），未执行点击',
          }
        }
        const [x1, y1, x2, y2] = decided.picked.bbox
        const cx = Math.max(0, Math.min(1000, Math.round((x1 + x2) / 2)))
        const cy = Math.max(0, Math.min(1000, Math.round((y1 + y2) / 2)))
        const clicked = await bridge.request(`/windows/${id}/click-auto`, owner, {
          body: { x: cx, y: cy, clicks, name: String(decided.picked.text || ''), ...windowWait(args), ...pinCapture(args.capture_id) },
          signal: exec.signal, timeoutMs: 60_000,
        })
        return {
          ...clicked,
          decided: {
            id: decided.id, text: decided.picked.text, type: decided.picked.type,
            bbox: decided.picked.bbox, score: decided.score,
            confidence: decided.confidence, table_source: decided.source,
            table_size: decided.table_size,
          },
        }
      }

      const x = unit(args.x, 'x')
      const y = unit(args.y, 'y')
      const dispatch = String(args.dispatch ?? 'input').toLowerCase()
      if (dispatch === 'message') {
        return bridge.request(`/windows/${id}/click-message`, owner, {
          body: { x, y, clicks, ...pinCapture(args.capture_id) }, signal: exec.signal,
        })
      }
      if (dispatch === 'auto') {
        const key = String(args.key ?? '').trim()
        const scope = String(args.scope ?? '').trim()
        let name = String(args.name ?? '').trim()
        if (!name && key) {
          const found = await kbResolve('desktop_control', key, scope, exec)
          if (found?.value) name = String(found.value)
        }
        const result = await bridge.request(`/windows/${id}/click-auto`, owner, {
          body: { x, y, clicks, name, ...windowWait(args), ...pinCapture(args.capture_id) },
          signal: exec.signal, timeoutMs: 60_000,
        })
        if (key && name && result?.success && !args.name) {
          if (await kbRemember('desktop_control', key, name, scope, exec)) result.kb_remembered = true
        }
        return result
      }
      if (dispatch !== 'input') throw new Error('dispatch must be input, message or auto')
      return bridge.request(`/windows/${id}/click`, ownerOf(exec), {
        body: { x, y, clicks, button, ...pinCapture(args.capture_id) }, signal: exec.signal,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_type',
    description: 'Enter literal text into a bound window. Prefer doraemon_invoke(action="set", value=...) when the UIA Edit control supports SetValue; otherwise use mode=paste (default), which uses the clipboard and avoids IME/key-layout conversion. Reserve mode=keys for short key sequences only; Chinese IME or keyboard layout can alter typed punctuation. After typing, verify the target Edit value through doraemon_uia before saving or submitting.',
    parameters: {
      window_id: windowIdParameter,
      text: { type: 'string', required: true, description: '1–2000 characters for paste, 1–500 for keys.' },
      mode: { type: 'string', description: 'paste (default) or keys.' },
      interval: { type: 'number', description: 'Seconds between keystrokes in keys mode, 0–1 (default 0.03).' },
      capture_id: captureParameter,
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const text = String(args.text ?? '')
      if (!text.trim()) throw new Error('text must not be blank')
      const mode = String(args.mode ?? 'paste').toLowerCase()
      if (mode === 'keys') {
        if (text.length > 500) throw new Error('keys mode accepts at most 500 characters')
        const interval = args.interval === undefined ? 0.03 : Number(args.interval)
        if (!Number.isFinite(interval) || interval < 0 || interval > 1) throw new Error('interval must be 0–1 seconds')
        return bridge.request(`/windows/${id}/keys`, ownerOf(exec), { body: { text, interval, ...pinCapture(args.capture_id) }, signal: exec.signal })
      }
      if (mode !== 'paste') throw new Error('mode must be paste or keys')
      if (text.length > 2000) throw new Error('paste mode accepts at most 2000 characters')
      return bridge.request(`/windows/${id}/type`, ownerOf(exec), { body: { text, ...pinCapture(args.capture_id) }, signal: exec.signal })
    },
  }))

  register(defineTool({
    name: 'doraemon_key',
    description: 'Press a key or combination in a bound window: enter, tab, escape, backspace, delete, home, end, pageup, pagedown, arrows, or combinations such as ctrl+a / ctrl+c / ctrl+v. Dangerous combinations (ctrl+w, alt+f4, win+l, win+r) are refused by the kernel. Enter only accepts count=1.',
    parameters: {
      window_id: windowIdParameter,
      key: { type: 'string', required: true, description: 'Single key or + separated combination, e.g. ctrl+a.' },
      count: { type: 'number', description: 'Repeat count, 1–50 (default 1).' },
      capture_id: captureParameter,
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const key = String(args.key ?? '').trim()
      if (!key) throw new Error('key must not be blank')
      const count = args.count === undefined ? 1 : Number(args.count)
      if (!Number.isInteger(count) || count < 1 || count > 50) throw new Error('count must be 1–50')
      return bridge.request(`/windows/${id}/key`, ownerOf(exec), { body: { key, count, ...pinCapture(args.capture_id) }, signal: exec.signal })
    },
  }))

  register(defineTool({
    name: 'doraemon_scroll',
    description: 'Scroll the wheel inside a bound window at normalized 0–1000 coordinates. Positive clicks scroll up or left, negative down or right.',
    parameters: {
      window_id: windowIdParameter,
      clicks: { type: 'number', required: true, description: 'Wheel clicks, -50–50; positive is up/left.' },
      x: { type: 'number', description: 'Normalized x, 0–1000 (default 500).' },
      y: { type: 'number', description: 'Normalized y, 0–1000 (default 500).' },
      direction: { type: 'string', description: 'vertical (default) or horizontal.' },
      capture_id: captureParameter,
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const clicks = Number(args.clicks)
      if (!Number.isInteger(clicks) || clicks < -50 || clicks > 50) throw new Error('clicks must be an integer between -50 and 50')
      const x = args.x === undefined ? 500 : unit(args.x, 'x')
      const y = args.y === undefined ? 500 : unit(args.y, 'y')
      const direction = String(args.direction ?? 'vertical').toLowerCase()
      if (direction !== 'vertical' && direction !== 'horizontal') throw new Error('direction must be vertical or horizontal')
      return bridge.request(`/windows/${id}/scroll`, ownerOf(exec), { body: { x, y, clicks, direction, ...pinCapture(args.capture_id) }, signal: exec.signal })
    },
  }))

  register(defineTool({
    name: 'doraemon_swipe',
    description: 'Drag from one normalized point to another inside a bound window, with a natural trajectory (sliders, drag-and-drop, text selection).',
    parameters: {
      window_id: windowIdParameter,
      x1: { type: 'number', required: true, description: 'Start x, 0–1000.' },
      y1: { type: 'number', required: true, description: 'Start y, 0–1000.' },
      x2: { type: 'number', required: true, description: 'End x, 0–1000.' },
      y2: { type: 'number', required: true, description: 'End y, 0–1000.' },
      speed: { type: 'number', description: 'Trajectory speed factor, 0.1–5 (default 1).' },
      capture_id: captureParameter,
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const x1 = unit(args.x1, 'x1')
      const y1 = unit(args.y1, 'y1')
      const x2 = unit(args.x2, 'x2')
      const y2 = unit(args.y2, 'y2')
      const speed = args.speed === undefined ? 1 : Number(args.speed)
      if (!Number.isFinite(speed) || speed < 0.1 || speed > 5) throw new Error('speed must be between 0.1 and 5')
      return bridge.request(`/windows/${id}/swipe`, ownerOf(exec), { body: { x1, y1, x2, y2, speed, ...pinCapture(args.capture_id) }, signal: exec.signal })
    },
  }))

  register(defineTool({
    name: 'doraemon_apps',
    description: 'Find or launch a local application through the kernel. action=find probes processes, windows, shortcuts and uninstall entries. action=launch starts a shortcut or system executable and confirms an attributable new window. If launch_uncertain is returned, inspect the existing windows before deciding what to do; do not blindly retry.',
    parameters: {
      action: { type: 'string', required: true, description: 'find | launch' },
      name: { type: 'string', description: 'App name or process name for action=find.' },
      shortcut_path: { type: 'string', description: 'Absolute .lnk path or built-in exe for action=launch; take it from action=find candidates.' },
    },
    output,
    async execute(args, exec) {
      const owner = ownerOf(exec)
      const action = String(args.action ?? '').toLowerCase()
      if (action === 'find') {
        const name = String(args.name ?? '').trim()
        if (!name) throw new Error('name is required for action=find')
        return bridge.request('/apps/find', owner, { body: { name }, signal: exec.signal, timeoutMs: 60_000 })
      }
      if (action === 'launch') {
        const shortcutPath = String(args.shortcut_path ?? '').trim()
        if (!shortcutPath) throw new Error('shortcut_path is required for action=launch')
        return bridge.request('/apps/launch', owner, { body: { shortcut_path: shortcutPath }, signal: exec.signal, timeoutMs: 60_000 })
      }
      throw new Error('Unknown action: use find or launch')
    },
  }))

  register(defineTool({
    name: 'doraemon_drag',
    description: 'Drag between two absolute screen pixels, crossing windows — for example dragging a file from the desktop into an application, or rearranging desktop icons. Unlike doraemon_swipe this is not window-bound: the start point may be the desktop or another app. The kernel follows a natural curve with jitter. Verify the drop with doraemon_analyze afterwards.',
    parameters: {
      x1: { type: 'number', required: true, description: 'Start x in screen pixels.' },
      y1: { type: 'number', required: true, description: 'Start y in screen pixels.' },
      x2: { type: 'number', required: true, description: 'End x in screen pixels.' },
      y2: { type: 'number', required: true, description: 'End y in screen pixels.' },
      speed: { type: 'number', description: 'Trajectory speed factor, 0.2–5 (default 1).' },
    },
    output,
    async execute(args, exec) {
      const point = (value, name) => {
        const number = Number(value)
        if (!Number.isInteger(number) || number < 0 || number > 20000) throw new Error(`${name} must be an integer screen pixel`)
        return number
      }
      const speed = args.speed === undefined ? 1 : Number(args.speed)
      if (!Number.isFinite(speed) || speed < 0.2 || speed > 5) throw new Error('speed must be between 0.2 and 5')
      return bridge.request('/drag', ownerOf(exec), {
        body: { x1: point(args.x1, 'x1'), y1: point(args.y1, 'y1'), x2: point(args.x2, 'x2'), y2: point(args.y2, 'y2'), speed },
        signal: exec.signal, timeoutMs: 60_000,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_clipboard',
    description: 'Read or write the system clipboard, or paste a file into a bound window (CF_HDROP, the equivalent of copying a file in Explorer). action=paste_file is the standard way to attach a file to a chat or mail composer; the target window must already be foreground.',
    parameters: {
      action: { type: 'string', required: true, description: 'get | set | paste_file' },
      text: { type: 'string', description: 'Clipboard text for action=set.' },
      window_id: { type: 'string', description: 'Bound window id for action=paste_file.' },
      path: { type: 'string', description: 'Absolute file path for action=paste_file.' },
    },
    output,
    async execute(args, exec) {
      const owner = ownerOf(exec)
      const action = String(args.action ?? '').toLowerCase()
      if (action === 'get') return bridge.request('/clipboard/text', owner, { signal: exec.signal })
      if (action === 'set') {
        const text = String(args.text ?? '')
        if (!text) throw new Error('text is required for action=set')
        return bridge.request('/clipboard/text', owner, { body: { text }, signal: exec.signal })
      }
      if (action === 'paste_file') {
        const id = boundWindowId(args.window_id)
        const path = String(args.path ?? '').trim()
        if (!path) throw new Error('path is required for action=paste_file')
        return bridge.request(`/windows/${id}/paste-file`, owner, { body: { path }, signal: exec.signal, timeoutMs: 30_000 })
      }
      throw new Error('Unknown action: use get, set or paste_file')
    },
  }))

  register(defineTool({
    name: 'doraemon_click_pixel',
    description: 'Click using pixel coordinates read off the saved screenshot (the SoM annotated image), converting them to the kernel’s normalized space. Use it when coordinates come from looking at the image rather than from ui_elements bboxes.',
    parameters: {
      window_id: windowIdParameter,
      image_x: { type: 'number', required: true, description: 'X in saved-image pixels.' },
      image_y: { type: 'number', required: true, description: 'Y in saved-image pixels.' },
      image_width: { type: 'number', required: true, description: 'Saved image width (from the analyze response).' },
      image_height: { type: 'number', required: true, description: 'Saved image height (from the analyze response).' },
      clicks: { type: 'number', description: 'Click count, 1–3 (default 1).' },
      capture_id: captureParameter,
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const clicks = args.clicks === undefined ? 1 : Number(args.clicks)
      if (!Number.isInteger(clicks) || clicks < 1 || clicks > 3) throw new Error('clicks must be 1–3')
      const pixel = (value, name) => {
        const number = Number(value)
        if (!Number.isInteger(number) || number < 0 || number > 100000) throw new Error(`${name} must be a non-negative image pixel`)
        return number
      }
      const body = {
        image_x: pixel(args.image_x, 'image_x'),
        image_y: pixel(args.image_y, 'image_y'),
        image_width: pixel(args.image_width, 'image_width'),
        image_height: pixel(args.image_height, 'image_height'),
        clicks,
      }
      if (body.image_width < 1 || body.image_height < 1) throw new Error('image_width and image_height must be positive')
      if (body.image_x >= body.image_width || body.image_y >= body.image_height) {
        throw new Error('image point must be inside image_width × image_height')
      }
      return bridge.request(`/windows/${id}/click-image-point`, ownerOf(exec), {
        body: { ...body, ...pinCapture(args.capture_id) }, signal: exec.signal,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_topmost',
    description: 'Pin a bound window above all others (TOPMOST) or restore its normal layer. Use it when another window covers the target, then restore immediately — a pinned window keeps covering everything.',
    parameters: {
      window_id: windowIdParameter,
      on: { type: 'boolean', description: 'true pins, false restores (default true).' },
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      return bridge.request(`/windows/${id}/topmost`, ownerOf(exec), {
        body: { on: args.on !== false }, signal: exec.signal,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_cleanup',
    description: 'Final desktop action for a task. The first desktop/browser operation captured a lightweight window baseline. This closes only windows whose launch was registered to this task, verifies the final window list, then unbinds this session’s controls. Existing windows are preserved; unowned new windows are reported rather than force-closed. Leave keep_window_keys empty unless the user should see a task-created window at the end; use the exact window_key from doraemon_windows. Set keep_browser=true only when the browser or its task-created pages are part of the requested final state. After cleanup_complete=true, do not retry cleanup or enumerate/close windows; report cleanup_success and any remaining_new_windows.',
    parameters: {
      close_browser: { type: 'boolean', description: 'Close browser resources created after the task baseline (default true).' },
      close_launched_apps: { type: 'boolean', description: 'Close task-created app windows (default true).' },
      keep_window_keys: { type: 'array', items: { type: 'string' }, description: 'Exact window_key values for task-created windows the user should see left open. Existing baseline windows are already preserved.' },
      keep_browser: { type: 'boolean', description: 'Keep the controlled browser and its task-created pages when they are part of the requested final state (default false).' },
    },
    output,
    async execute(args, exec) {
      return bridge.request('/cleanup', ownerOf(exec), {
        body: {
          close_browser: args.close_browser !== false,
          close_launched_apps: args.close_launched_apps !== false,
          keep_window_keys: Array.isArray(args.keep_window_keys) ? args.keep_window_keys : [],
          keep_browser: args.keep_browser === true,
        },
        signal: exec.signal, timeoutMs: 60_000,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_parser',
    description: 'Diagnose the kernel’s visual parser: which engine is configured, whether the parser service answers, and its advertised capabilities. No screenshot is taken.',
    parameters: {}, output,
    async execute(_args, exec) {
      return bridge.request('/parser/status', ownerOf(exec), { signal: exec.signal })
    },
  }))

  register(defineTool({
    name: 'doraemon_files',
    description: 'Create, append to, delete or list files the kernel session owns inside its agent workspace. Deletion is limited to files this session created — it is not a general filesystem tool.',
    parameters: {
      action: { type: 'string', required: true, description: 'create | write | delete | list' },
      name: { type: 'string', description: 'Relative file name for action=create.' },
      path: { type: 'string', description: 'Absolute path for action=write/delete.' },
      content: { type: 'string', description: 'File content for action=create/write.' },
      append: { type: 'boolean', description: 'Append instead of overwrite (action=write, default false).' },
    },
    output,
    async execute(args, exec) {
      const owner = ownerOf(exec)
      const action = String(args.action ?? '').toLowerCase()
      if (action === 'list') return bridge.request('/files', owner, { signal: exec.signal })
      if (action === 'create') {
        const name = String(args.name ?? '').trim()
        if (!name) throw new Error('name is required for action=create')
        return bridge.request('/files/create', owner, { body: { name, content: String(args.content ?? '') }, signal: exec.signal })
      }
      if (action === 'write' || action === 'delete') {
        const path = String(args.path ?? '').trim()
        if (!path) throw new Error('path is required for action=write/delete')
        if (action === 'delete') return bridge.request('/files/delete', owner, { body: { path }, signal: exec.signal })
        return bridge.request('/files/write', owner, {
          body: { path, content: String(args.content ?? ''), append: args.append === true }, signal: exec.signal,
        })
      }
      throw new Error('Unknown action: use create, write, delete or list')
    },
  }))

  register(defineTool({
    name: 'doraemon_invoke',
    description: 'Use UI Automation to activate or set a control without mouse coordinates. For native buttons/menus use Invoke; for Edit controls prefer action=set with value to avoid keyboard focus and IME issues. Targets by an exact control name from doraemon_uia, optionally filtered by control_type. Returns effect confirmed/suspected_noop/unverifiable/refused; read the value back before saving/submitting.',
    parameters: {
      window_id: windowIdParameter,
      name: { type: 'string', description: 'Control name as reported by doraemon_uia, e.g. "发送". Optional when key is given.' },
      control_type: { type: 'string', description: 'Optional UIA control type filter, e.g. Button / TabItem / Edit.' },
      value: { type: 'string', description: 'Text to set when the control is an edit field.' },
      action: { type: 'string', description: 'auto (default) | invoke | toggle | set | select | expand.' },
      key: { type: 'string', description: 'Semantic key resolved from the knowledge base when name is missing, e.g. "发送按钮".' },
      scope: { type: 'string', description: 'Scope for the key lookup, usually the app exe; empty means global.' },
      capture_id: captureParameter,
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const key = String(args.key ?? '').trim()
      const scope = String(args.scope ?? '').trim()
      let name = String(args.name ?? '').trim()
      if (!name && key) {
        const found = await kbResolve('desktop_control', key, scope, exec)
        if (found?.value) name = String(found.value)
      }
      if (!name) throw new Error('name is required (or a key that resolves to one)')
      const action = String(args.action ?? 'auto').toLowerCase()
      if (!['auto', 'invoke', 'toggle', 'set', 'select', 'expand'].includes(action)) {
        throw new Error('action must be auto, invoke, toggle, set, select or expand')
      }
      const result = await bridge.request(`/windows/${id}/invoke`, ownerOf(exec), {
        body: {
          name,
          control_type: String(args.control_type ?? ''),
          value: String(args.value ?? ''),
          action,
          ...pinCapture(args.capture_id),
        },
        signal: exec.signal, timeoutMs: 60_000,
      })
      if (key && result?.success && !args.name) {
        if (await kbRemember('desktop_control', key, name, scope, exec)) result.kb_remembered = true
      }
      return result
    },
  }))

  register(defineTool({
    name: 'doraemon_capture',
    description: 'Escalation-only visual evidence: capture a bound window when UIA/OCR cannot resolve an ambiguity or when an overlay may have stolen focus. Returns a PNG path plus captured_with (printwindow | screen) and occlusion status. Use the image-reading tool only when the current model supports image input; otherwise return to doraemon_analyze for OCR/YOLO evidence.',
    parameters: { window_id: windowIdParameter },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      return bridge.request(`/windows/${id}/capture`, ownerOf(exec), {
        body: {}, signal: exec.signal, timeoutMs: 60_000,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_wait_stable',
    description: 'Wait until the window stops changing, instead of sleeping a fixed time: returns as soon as the screen has been quiet for quiet_ms (streaming output keeps it waiting). Purely pixel-based, so it works for web/Electron, Qt and custom-drawn UIs alike; each probe is ~50ms of local work and costs no model tokens. Call it after sending a message or navigating, then run doraemon_analyze once.',
    parameters: {
      window_id: windowIdParameter,
      quiet_ms: { type: 'number', description: 'How long the screen must stay unchanged, 200–5000 (default 600).' },
      timeout_ms: { type: 'number', description: 'Give up after this long, 1000–120000 (default 20000).' },
      threshold: { type: 'number', description: 'Per-probe mean brightness delta treated as "still", 0.1–50 (default 1.2).' },
      max_changed_cells: { type: 'number', description: 'How many 48×48 cells may change and still count as still, 0–1000 (default 10). Raise it for blinking carets, lower it for tiny spinners.' },
      interval_ms: { type: 'number', description: 'Probe spacing, 50–1000 (default 150).' },
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const number = (value, fallback, min, max, name) => {
        const parsed = value === undefined ? fallback : Number(value)
        if (!Number.isFinite(parsed) || parsed < min || parsed > max) {
          throw new Error(`${name} must be ${min}–${max}`)
        }
        return parsed
      }
      const quiet_ms = number(args.quiet_ms, 600, 200, 5000, 'quiet_ms')
      const timeout_ms = number(args.timeout_ms, 20000, 1000, 120000, 'timeout_ms')
      const threshold = number(args.threshold, 1.2, 0.1, 50, 'threshold')
      const max_changed_cells = number(args.max_changed_cells, 10, 0, 1000, 'max_changed_cells')
      const interval_ms = number(args.interval_ms, 150, 50, 1000, 'interval_ms')
      return bridge.request(`/windows/${id}/wait-stable`, ownerOf(exec), {
        body: { quiet_ms, timeout_ms, threshold, max_changed_cells, interval_ms },
        signal: exec.signal,
        timeoutMs: timeout_ms + 20000,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_cursor',
    description: 'Show or move a cosmetic overlay cursor: transparent, always on top, click-through, and it never moves the physical mouse. Give window_id plus normalized x/y, or raw screen pixels with window_id omitted. visible=false hides it. This is purely visual — it does not click anything; once shown, subsequent clicks move it to the action point.',
    parameters: {
      window_id: { type: 'string', description: 'Bound window id; omit to use raw screen coordinates.' },
      x: { type: 'number', description: 'Normalized 0–1000 within window_id, or screen pixel x without it.' },
      y: { type: 'number', description: 'Normalized 0–1000 within window_id, or screen pixel y without it.' },
      visible: { type: 'boolean', description: 'false hides the overlay (default true).' },
    },
    output,
    async execute(args, exec) {
      const visible = args.visible !== false
      let windowId = ''
      let x = -1
      let y = -1
      if (visible) {
        x = Number(args.x)
        y = Number(args.y)
        if (Number.isFinite(x) && Number.isFinite(y)) {
          if (String(args.window_id ?? '').trim()) {
            windowId = boundWindowId(args.window_id)
          } else {
            x = Math.trunc(x)
            y = Math.trunc(y)
          }
        } else {
          throw new Error('x and y are required when visible=true')
        }
      }
      return bridge.request('/cursor', ownerOf(exec), {
        body: { window_id: windowId, x: visible ? Math.trunc(x) : -1, y: visible ? Math.trunc(y) : -1, visible },
        signal: exec.signal,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_highlight',
    description: 'Draw a green glow around the window currently being operated (transparent, topmost, click-through). Every action refreshes it automatically and it fades out a few seconds after the last one; on=false hides it immediately. Purely visual.',
    parameters: {
      window_id: windowIdParameter,
      on: { type: 'boolean', description: 'true shows/refreshes the glow (default), false hides it.' },
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      return bridge.request(`/windows/${id}/highlight`, ownerOf(exec), {
        body: { on: args.on !== false }, signal: exec.signal,
      })
    },
  }))

  const WEB_ACTIONS = ['open', 'snapshot', 'dom', 'click', 'type', 'select', 'key', 'scroll',
    'hover', 'extract', 'wait', 'status', 'close', 'check', 'settle', 'batch', 'lease',
    'action_status']
  // dom takes a CSS query (not a snapshot selector), but it is still a selector-shaped action.
  const SELECTOR_ACTIONS = new Set(['click', 'type', 'select', 'hover', 'wait', 'dom', 'check'])

  /** Resolve a semantic key through the local knowledge index (empty when unknown). */
  async function kbResolve(kind, key, scope, exec) {
    if (!key) return null
    try {
      const found = await bridge.request('/kb/resolve', ownerOf(exec), {
        body: { kind, key, scope: scope ?? '' }, signal: exec.signal, timeoutMs: 10_000,
      })
      return found?.found ? found : null
    } catch {
      return null
    }
  }

  async function kbRemember(kind, key, value, scope, exec) {
    if (!key || !value) return false
    try {
      const saved = await bridge.request('/kb/remember', ownerOf(exec), {
        body: { kind, key, value, scope: scope ?? '' }, signal: exec.signal, timeoutMs: 10_000,
      })
      return Boolean(saved?.success)
    } catch {
      return false
    }
  }

  register(defineTool({
    name: 'doraemon_web',
    description: 'Drive the controlled Chromium (persistent profile, so logins survive). Workflow: action="open" → action="snapshot" to get selectors → act → snapshot again after any click or navigation. Selectors must come from snapshot; never invent one. Every action returns {success, action, tool, ...}. eval_js is deliberately not exposed. Pass key="登录按钮" (plus scope=host) to address an element by meaning: the key is looked up in the knowledge base when selector/url is missing, and the mapping is saved after a success.',
    parameters: {
      action: { type: 'string', required: true, description: WEB_ACTIONS.join(' | ') },
      url: { type: 'string', description: 'action=open: the page to open.' },
      selector: { type: 'string', description: 'Selector reported by action=snapshot. For action=dom: a CSS query, e.g. [class*="el-button"] — returns {tag,class,text,rect} for each match, which is how you inspect components that have no role (custom selects, cards, tables) and therefore never appear in a snapshot.' },
      text: { type: 'string', description: 'action=type: text to fill in.' },
      value: { type: 'string', description: 'action=select: option value or visible text.' },
      key_name: { type: 'string', description: 'action=key: key to press, e.g. Enter or Control+a.' },
      key: { type: 'string', description: 'Semantic key for the target, e.g. "登录按钮" or "订单页".' },
      scope: { type: 'string', description: 'Scope of the key, usually the site host; leave empty for global.' },
      pixels: { type: 'number', description: 'action=scroll: pixels, positive scrolls down.' },
      max_elements: { type: 'number', description: 'action=snapshot: 10–200 elements (default 60).' },
      max_chars: { type: 'number', description: 'action=extract: 100–20000 chars (default 4000).' },
      timeout_ms: { type: 'number', description: 'action=open/wait timeout in ms (default 30000).' },
      press_enter: { type: 'boolean', description: 'action=type: press Enter after filling.' },
      // ── batch：一次调用按顺序执行多步（只用于已验证路径的回放，不用于探索） ──
      steps: {
        type: 'array',
        description: 'action=batch: ordered steps, each {action, selector, nth, text, key_name, seconds, allow_mutating, verify}. '
          + 'Whitelist: click/type/key/hover/check/scroll/wait/select/focus/dom. '
          + 'At most ONE write step and it must be the LAST one — otherwise the batch would commit before you can inspect intermediate state.',
      },
      stop_on_error: { type: 'boolean', description: 'action=batch: stop at the first failing step (default true).' },
      evidence: { type: 'string', description: 'action=batch: none | dom (default). dom returns element count + text/input-value hash.' },
      budget_ms: { type: 'number', description: 'action=batch: total budget in ms (default 150000).' },
      allow_mutating: { type: 'boolean', description: 'action=batch/click: required (with owner) for any write step.' },
      verify: { type: 'string', description: 'action=click/batch: what should be true after the write, e.g. "列表出现 dsh-test-xxx".' },
      target_text: { type: 'string', description: 'action=click: 破坏性操作（删除/停用/清空等）必须声明要操作对象的名字或标识。内核会验证它在页面上恰好只有 1 个可见匹配，不满足直接拒绝——这是"先搜索、筛到只剩一条"约定的机器版。' },
      commit: { type: 'string', description: 'action=click: set "local" for a submit that only fills the current form (no request), e.g. a transfer dialog 确定. The kernel verifies the claim against the dialog text and refuses if it contains destructive words. A local commit does not consume the batch\'s single write step.' },
      // ── 租约：同一浏览器同一时刻只允许一个会话驱动 ──
      op: { type: 'string', description: 'action=lease: acquire | renew | release | status (default status). Write actions acquire automatically when lease_auto is on.' },
      ttl_s: { type: 'number', description: 'action=lease: lease lifetime in seconds, 10–3600 (default 120). Renewed automatically on every write.' },
      force: { type: 'boolean', description: 'action=lease op=release: release even if another session holds it (audited).' },
      lease_auto: { type: 'boolean', description: 'Default true: a write action (allow_mutating) acquires/renews the lease for this session automatically. Set false to manage the lease yourself via action=lease.' },
      action_id: { type: 'string', description: 'action=action_status: the id returned by a call that timed out (result_unknown). Ask whether it is still running instead of re-issuing the write.' },
    },
    output,
    async execute(args, exec) {
      const action = String(args.action ?? '').toLowerCase()
      if (!WEB_ACTIONS.includes(action)) throw new Error(`action must be one of ${WEB_ACTIONS.join(', ')}`)
      const body = { action }
      for (const field of ['url', 'text', 'value', 'key_name']) {
        if (args[field] !== undefined) body[field] = String(args[field])
      }
      for (const field of ['pixels', 'max_elements', 'max_chars', 'timeout_ms']) {
        if (args[field] !== undefined) {
          const parsed = Number(args[field])
          if (!Number.isFinite(parsed)) throw new Error(`${field} must be a number`)
          body[field] = parsed
        }
      }
      if (args.press_enter !== undefined) body.press_enter = Boolean(args.press_enter)

      // batch 透传：steps 只做形状校验，动作白名单由内核强制（两边都卡一道）。
      if (args.steps !== undefined) {
        if (!Array.isArray(args.steps) || args.steps.length === 0 || args.steps.length > 40) {
          throw new Error('steps must be an array of 1–40 items')
        }
        body.steps = args.steps.map((step, index) => {
          if (!step || typeof step !== 'object' || typeof step.action !== 'string' || !step.action) {
            throw new Error(`steps[${index}] must be an object with an action`)
          }
          return step
        })
      }
      if (args.stop_on_error !== undefined) body.stop_on_error = Boolean(args.stop_on_error)
      if (args.evidence !== undefined) body.evidence = String(args.evidence)
      if (args.budget_ms !== undefined) {
        const budget = Number(args.budget_ms)
        if (!Number.isFinite(budget)) throw new Error('budget_ms must be a number')
        body.budget_ms = budget
      }
      if (args.allow_mutating !== undefined) body.allow_mutating = Boolean(args.allow_mutating)
      if (args.verify !== undefined) body.verify = String(args.verify)
      if (args.commit !== undefined) body.commit = String(args.commit)
      if (args.target_text !== undefined) body.target_text = String(args.target_text)
      if (action === 'batch' && !body.steps) throw new Error('batch needs steps')

      const key = String(args.key ?? '').trim()
      const scope = String(args.scope ?? '').trim()
      let selector = String(args.selector ?? '').trim()
      let url = String(args.url ?? '').trim()

      // ── 租约：写动作自动持有 ────────────────────────────────────────────
      // 同一浏览器同一时刻只允许一个会话驱动：两个写者交替操作会产生"陈旧 DOM"
      // （双方读到的都可能是过期状态，我们实际误判过两次）。
      const owner = ownerOf(exec)
      if (action === 'lease') {
        return bridge.request('/web', owner, {
          body: {
            action: 'lease',
            op: String(args.op ?? 'status').toLowerCase(),
            owner,
            ttl_s: Number(args.ttl_s ?? 120),
            force: Boolean(args.force),
          },
          signal: exec.signal,
        })
      }
      // Minimal "is that action still running?" path: a timed-out write must be
      // re-checked here instead of being re-issued.
      if (action === 'action_status') {
        const actionId = String(args.action_id ?? '').trim()
        if (!actionId) throw new Error('action_status needs action_id (returned by a timed-out call)')
        return bridge.request(`/web/actions/${encodeURIComponent(actionId)}`, owner, {
          signal: exec.signal, timeoutMs: 15_000,
        })
      }
      const wantsLease = (args.lease_auto !== false) && (
        action === 'open' || action === 'close' || body.allow_mutating === true
        || (Array.isArray(body.steps) && body.steps.some((s) => s?.allow_mutating)))
      let acquiredHere = false
      if (wantsLease) {
        // 拿不到就如实返回占用情况，不做重试轰炸，也不静默降级成"无租约执行"。
        const lease = await bridge.request('/web', owner, {
          body: { action: 'lease', op: 'acquire', owner, ttl_s: Number(args.ttl_s ?? 120) },
          signal: exec.signal,
        })
        if (!lease?.held) {
          return {
            success: false, action, tool: 'doraemon_web', lease_conflict: true,
            holder: lease?.holder ?? null, expires_in_s: lease?.expires_in_s ?? null,
            error: lease?.error ?? {
              code: 'LEASE_HELD',
              hint: '另一个会话正在驱动浏览器：等待对方释放，或用 action=lease op=release force=true（会记审计）',
            },
          }
        }
        acquiredHere = true
      }
      // 本次调用自己拿到的租约，在 finally 里释放：会话关闭/异常都不会留下陈旧租约
      // （内核 TTL 仍作为兜底，进程被 kill 时靠它过期）。
      try {

      // Knowledge-base seam: a key can stand in for the selector/url.
      if (key) {
        if (action === 'open' && !url) {
          const found = await kbResolve('web_page', key, scope, exec)
          if (found?.value) url = String(found.value)
        } else if (SELECTOR_ACTIONS.has(action) && !selector) {
          const found = await kbResolve('web_selector', key, scope, exec)
          if (found?.value) selector = String(found.value)
        }
      }
      if (url) body.url = url
      if (selector) body.selector = selector
      if (action === 'open' && !body.url) throw new Error('open needs url (or a key that resolves to one)')
      if (SELECTOR_ACTIONS.has(action) && !body.selector) {
        throw new Error(`${action} needs selector (or a key that resolves to one)`)
      }

      // 内核的写闸门按 owner 校验租约；batch 内每个写步骤继承它。
      body.owner = owner
      // The kernel owns the deadline. The HTTP client must wait longer than the
      // batch budget, otherwise it aborts first and the worker keeps executing
      // while nobody is listening (the old default: 60s client vs 150s budget).
      const budgetMs = Number.isFinite(body.budget_ms) ? Number(body.budget_ms) : 150_000
      const clientTimeoutMs = action === 'batch'
        ? budgetMs + 20_000
        // Single actions: the kernel waits timeout_ms + 30s, so the client must
        // outlast that or it would abort while the page action is still running.
        : Math.max(60_000, Number(body.timeout_ms || 0) + 35_000)
      const result = await bridge.request('/web', owner, {
        body, signal: exec.signal, timeoutMs: clientTimeoutMs,
      })
      if (key && result?.success) {
        const saved = action === 'open'
          ? await kbRemember('web_page', key, String(body.url), scope, exec)
          : (SELECTOR_ACTIONS.has(action)
            ? await kbRemember('web_selector', key, String(body.selector), scope, exec)
            : false)
        if (saved) result.kb_remembered = true
      }
      return result
      } finally {
        if (acquiredHere) {
          // 释放不带 signal：调用被取消时也要尽力释放，否则别人要等到 TTL 过期。
          await bridge.request('/web', owner, {
            body: { action: 'lease', op: 'release', owner },
          }).catch(() => {})
        }
      }
    },
  }))

  if (config.enableExperimentalPageCache === true) register(defineTool({
    name: 'doraemon_page_cache',
    description: 'Opt-in cache for a verified UIA navigation step. lookup returns advisory hints only; navigate independently checks the current foreground, exact target and postcondition, then executes once and records only verified success. Never use to retry an uncertain submit, send or destructive action.',
    parameters: {
      window_id: windowIdParameter,
      op: { type: 'string', required: true, description: 'lookup | navigate' },
      target: { type: 'string', required: true, description: 'Exact live UIA target name.' },
      control_type: { type: 'string', required: true, description: 'Exact UIA control type.' },
      expected: { type: 'string', required: true, description: 'Exact UIA name absent before and uniquely present after navigation.' },
      method: { type: 'string', description: 'click | invoke (default click).' },
      enabled: { type: 'boolean', description: 'true enables cache reads and writes; omitted or false leaves caching off.' },
    },
    output,
    async execute(args, exec) {
      const id = boundWindowId(args.window_id)
      const op = String(args.op ?? '').toLowerCase()
      const method = String(args.method ?? 'click').toLowerCase()
      const target = String(args.target ?? '').trim()
      const controlType = String(args.control_type ?? '').trim()
      const expected = String(args.expected ?? '').trim()
      if (!['lookup', 'navigate'].includes(op) || !['click', 'invoke'].includes(method)) throw new Error('Invalid cache operation or method')
      if (!target || target.length > 120 || !controlType || controlType.length > 40 || !expected || expected.length > 120) throw new Error('target, control_type and expected are required')
      return bridge.request(`/windows/${id}/page-cache`, ownerOf(exec), {
        body: { op, target, control_type: controlType, expected, method, enabled: args.enabled === true },
        signal: exec.signal, timeoutMs: 90_000,
      })
    },
  }))

  register(defineTool({
    name: 'doraemon_kb',
    description: 'Read and write the local knowledge base: BM25 search over kb/corrections/success_paths, plus the structured key index that web and desktop actions resolve through. op=search | resolve | remember | list. Use remember to pin "meaning → selector/control" so later calls can pass key= alone.',
    parameters: {
      op: { type: 'string', required: true, description: 'search | resolve | remember | list' },
      query: { type: 'string', description: 'op=search: query text.' },
      kind: { type: 'string', description: 'op=resolve/remember/list: web_selector | web_page | desktop_control.' },
      key: { type: 'string', description: 'op=resolve/remember: the semantic key.' },
      value: { type: 'string', description: 'op=remember: selector, URL or control name.' },
      scope: { type: 'string', description: 'op=resolve/remember: host or app exe; empty means global.' },
      source: { type: 'string', description: 'op=search: all | kb | corrections | success_paths | index.' },
      top_k: { type: 'number', description: 'op=search: 1–20 (default 4).' },
      limit: { type: 'number', description: 'op=list: 1–1000 (default 200).' },
    },
    output,
    async execute(args, exec) {
      const op = String(args.op ?? '').toLowerCase()
      const owner = ownerOf(exec)
      if (op === 'search') {
        const query = String(args.query ?? '').trim()
        if (!query) throw new Error('search needs query')
        return bridge.request('/kb/search', owner, {
          body: { query, top_k: Number(args.top_k ?? 4), source: String(args.source ?? 'all') },
          signal: exec.signal,
        })
      }
      if (op === 'resolve') {
        const kind = String(args.kind ?? '').trim()
        const key = String(args.key ?? '').trim()
        if (!kind || !key) throw new Error('resolve needs kind and key')
        return bridge.request('/kb/resolve', owner, {
          body: { kind, key, scope: String(args.scope ?? '') }, signal: exec.signal,
        })
      }
      if (op === 'remember') {
        const kind = String(args.kind ?? '').trim()
        const key = String(args.key ?? '').trim()
        const value = String(args.value ?? '').trim()
        if (!kind || !key || !value) throw new Error('remember needs kind, key and value')
        return bridge.request('/kb/remember', owner, {
          body: { kind, key, value, scope: String(args.scope ?? '') }, signal: exec.signal,
        })
      }
      if (op === 'list') {
        return bridge.request('/kb/list', owner, {
          body: { kind: String(args.kind ?? ''), limit: Number(args.limit ?? 200) }, signal: exec.signal,
        })
      }
      throw new Error('op must be search, resolve, remember or list')
    },
  }))

  register(defineTool({
    name: 'doraemon_health',
    description: 'Check local task bridge availability without capturing the screen or invoking a model. Starts the local kernel in the background (hidden, no window) when it is not already running, then reports its health, protocol and whether task execution is enabled.',
    parameters: {}, output,
    async execute(_args, exec) {
      const health = await bridge.request('/health', ownerOf(exec), { signal: exec.signal })
      bridge.kernel.observeHealth(health)
      const info = bridge.kernel.describe()
      // The kernel may have been restarted while this plugin stayed loaded.
      // Its live health response is authoritative for the process id.
      const bridgeInfo = info ? { ...info, pid: health?.bridge?.pid ?? info.pid } : health?.bridge ?? null
      if (health.execution_enabled) return { ...health, bridge: bridgeInfo }
      return {
        ...health,
        bridge: bridgeInfo,
        hint: 'The kernel was started without an API key, so task submission is refused. Configure the DeepSeek key in this DSH window (Settings → Models) and call this again; the plugin reads it from ctx.credentials, and replaces a keyless kernel it started itself.',
      }
    },
  }))
  console.info('[doraemon] %d tools registered (%s); loopback port=%d; autoStart=%s; env token available=%s',
    registered.length, registered.join(','), config.port, String(config.autoStart), Boolean(process.env[config.tokenEnv]))
}
