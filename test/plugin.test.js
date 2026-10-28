import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import path, { delimiter, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply, Config, createToolTimeline } from '../index.js'
import { createBridge, extractFrames, ownerOf } from '../bridge-client.js'
import { createKernel } from '../kernel.js'

const token = 'test-only-bridge-token-not-real-credentials'

function setup(options = {}) {
  process.env.DSH_DESKTOP_BRIDGE_TOKEN = token
  const tools = new Map(), effects = []
  apply({ tools: { register: tool => tools.set(tool.name, tool) }, effect: effect => effects.push(effect) },
    Config({ waitSeconds: 1, manageKernel: false, ...options }))
  return { tools, effects }
}

function execution(id = 'session-1') {
  return { agent: { session: { id } }, callId: 'call-1', signal: new AbortController().signal }
}


test('shipped files carry no customer, endpoint or machine-local references', async () => {
  // This plugin is ported out of an internal project: nothing customer-specific
  // (product names, intranet hosts, absolute user paths) may ride along.
  const shipped = ['index.js', 'kernel.js', 'bridge-client.js', 'cordis.patch.yml', 'package.json']
  const banned = ['ioa', 'tencent', '企业微信', 'wemail', 'venus', 'workbuddy', '云智',
    '内网', '10.91', 'oa.com', 'c:\\users\\', '/users/', 'administrator']
  for (const file of shipped) {
    const text = (await readFile(new URL(`../${file}`, import.meta.url), 'utf8')).toLowerCase()
    for (const term of banned) {
      assert.ok(!text.includes(term), `${file} must not contain "${term}"`)
    }
  }
})


test('real 0.2.1-rc.2 SDK registers every tool with canonical output schemas', () => {
  const { tools } = setup()
  // 30 until doraemon_click_screen was added for popups that live in their own
  // top-level window and therefore fall outside the bound window's client rect.
  assert.equal(tools.size, 31)
  for (const tool of tools.values()) assert.equal(tool.output.schema.type, 'object')
})

test('health preserves the live kernel PID for externally managed kernels', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async () => Response.json({ status: 'ok', execution_enabled: true, bridge: { pid: 4321 } })
  try {
    const { tools } = setup()
    const result = await tools.get('doraemon_health').execute({}, execution())
    assert.equal(result.bridge.pid, 4321)
  } finally { globalThis.fetch = original }
})

test('health reports live PID after a cached kernel is restarted', async () => {
  const stateDir = mkdtempSync(join(tmpdir(), 'dsh-health-pid-'))
  writeFileSync(join(stateDir, 'kernel-state.json'), JSON.stringify({ port: 8766, token, pid: 111 }))
  const original = globalThis.fetch
  let calls = 0
  globalThis.fetch = async () => Response.json({ status: 'ok', protocol: 1, execution_enabled: true, bridge: { pid: ++calls === 1 ? 111 : 222 } })
  try {
    const { tools } = setup({ manageKernel: true, autoStart: false, stateDir })
    const result = await tools.get('doraemon_health').execute({}, execution())
    assert.equal(result.bridge.pid, 222)
  } finally {
    globalThis.fetch = original
    rmSync(stateDir, { recursive: true, force: true })
  }
})

test('UIA target hints reach the kernel for progressive OCR fallback', async () => {
  const original = globalThis.fetch
  let call
  globalThis.fetch = async (url, init) => {
    call = { url: String(url), body: JSON.parse(init.body) }
    return Response.json({ success: true, method: 'uia+omniparser', target_found: false })
  }
  try {
    const { tools } = setup()
    await tools.get('doraemon_uia').execute({
      window_id: 'win-0123456789', target_hint: '文件名', target_kind: 'control', max_attempts: 2,
    }, execution())
    assert.equal(call.url, 'http://127.0.0.1:8766/api/dsh/windows/win-0123456789/uia')
    assert.deepEqual(call.body, {
      max_depth: 7, max_nodes: 120, target_hint: '文件名', target_kind: 'control', max_attempts: 2,
    })
  } finally { globalThis.fetch = original }
})

test('UIA batch makes one bridge call for a stable in-window sequence', async () => {
  const original = globalThis.fetch
  let call
  globalThis.fetch = async (url, init) => {
    call = { url: String(url), body: JSON.parse(init.body) }
    return Response.json({ success: true, uia_scans: 1, steps: [{ ok: true }, { ok: true }] })
  }
  try {
    const { tools } = setup()
    const steps = [
      { action: 'click', name: '搜索框' },
      { action: 'type', name: '搜索框', text: 'settings' },
    ]
    const result = await tools.get('doraemon_uia_batch').execute({ window_id: 'win-0123456789', steps }, execution())
    assert.equal(result.uia_scans, 1)
    assert.equal(call.url, 'http://127.0.0.1:8766/api/dsh/windows/win-0123456789/uia-batch')
    assert.deepEqual(call.body, { steps, max_depth: 10, max_nodes: 600, timeout_s: 30 })
    await assert.rejects(
      tools.get('doraemon_uia_batch').execute({ window_id: 'win-0123456789', steps: [{ action: 'click' }] }, execution()),
      /exact name or auto_id/)
  } finally { globalThis.fetch = original }
})


test('window tools forward normalized coordinates and reject out-of-range input', async () => {
  const original = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return new Response('{"success":true}')
  }
  try {
    const { tools } = setup()
    await tools.get('doraemon_click').execute(
      { window_id: 'win-0123456789', x: 120, y: 340 }, execution())
    assert.equal(calls[0].url, 'http://127.0.0.1:8766/api/dsh/windows/win-0123456789/click')
    assert.deepEqual(JSON.parse(calls[0].options.body), { x: 120, y: 340, clicks: 1, button: 'left' })

    await assert.rejects(
      tools.get('doraemon_click').execute({ window_id: 'win-0123456789', x: 1001, y: 0 }, execution()),
      /0–1000/)
    await assert.rejects(
      tools.get('doraemon_click').execute({ window_id: '../etc', x: 10, y: 10 }, execution()),
      /window_id/)
  } finally { globalThis.fetch = original }
})

test('owner is bound to stable session.id, not model arguments', () => {
  assert.match(ownerOf(execution()), /^[a-f0-9]{64}$/)
  assert.notEqual(ownerOf(execution('other-session')), ownerOf(execution()))
  assert.throws(() => ownerOf({}), /session identity/)
})

test('loopback client does not accept remote hosts, paths or redirects', async () => {
  assert.throws(() => createBridge({ port: 80 }), /port/)
  let options, url
  const bridge = createBridge({ port: 8766, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', manageKernel: false }, async (u, o) => {
    url = u; options = o
    return new Response('{"status":"ok"}')
  })
  await bridge.request('/health', ownerOf(execution()))
  assert.equal(url, 'http://127.0.0.1:8766/api/dsh/health')
  assert.equal(options.redirect, 'error')
  assert.equal(options.headers.Authorization, `Bearer ${token}`)
  await assert.rejects(bridge.request('/../health', 'a'), /path/)
  assert.throws(() => bridge.taskPath('../other'), /task id/)
})

test('missing secret rejects without sending a request', async () => {
  delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
  const bridge = createBridge({ port: 8766, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', manageKernel: false }, () => { throw new Error('unexpected network') })
  await assert.rejects(bridge.request('/health', 'a'), /Set DSH_DESKTOP_BRIDGE_TOKEN/)
})

test('pre-aborted call never starts desktop work', async () => {
  const { tools } = setup()
  const exec = execution()
  exec.signal = AbortSignal.abort(new Error('cancelled'))
  await assert.rejects(tools.get('doraemon_run').execute({ task: 'test' }, exec), /cancelled/)
})

test('run returns actual outcome and passes stable request identity', async () => {
  const original = globalThis.fetch
  const calls = [], taskId = 'a'.repeat(32)
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return new Response(JSON.stringify(url.endsWith('/tasks') ? { task_id: taskId, session_id: 's1' }
      : { task_id: taskId, status: 'finished', result: { task_outcome: 'unknown' } }))
  }
  try {
    const { tools } = setup()
    const value = await tools.get('doraemon_run').execute({ task: 'test', request_id: 'occurrence-1' }, execution())
    assert.equal(value.result.task_outcome, 'unknown')
    assert.equal(JSON.parse(calls[0].options.body).request_id, 'occurrence-1')
    assert.equal(calls[0].options.headers['X-DSH-Owner'], ownerOf(execution()))
  } finally { globalThis.fetch = original }
})

test('in-flight cancellation requests stop independently of cancelled signal', async () => {
  const exec = execution(), controller = new AbortController()
  exec.signal = controller.signal
  const original = globalThis.fetch
  const calls = [], taskId = 'b'.repeat(32)
  globalThis.fetch = async (url, options) => {
    calls.push(url)
    if (url.endsWith('/tasks')) return new Response(JSON.stringify({ task_id: taskId }))
    if (url.endsWith('/stop')) {
      assert.equal(options.signal.aborted, false)
      return new Response('{"stop_requested":true}')
    }
    controller.abort(new Error('cancelled during work'))
    throw controller.signal.reason
  }
  try {
    const { tools } = setup()
    await assert.rejects(tools.get('doraemon_run').execute({ task: 'test' }, exec), /cancelled during work/)
    assert.equal(calls.at(-1), `http://127.0.0.1:8766/api/dsh/tasks/${taskId}/stop`)
  } finally { globalThis.fetch = original }
})

test('kernel lifecycle stays inert until a tool call asks for it', async () => {
  delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
  const kernel = createKernel({ port: 8766, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', autoStart: true, bootSeconds: 5 })
  assert.equal(kernel.describe(), null)
  assert.equal(kernel.ready, false)
  assert.equal(await kernel.stop(), false)
  assert.equal(kernel.token, '')
})

test('kernel root defaults to the installed plugin directory', () => {
  const kernel = createKernel({ port: 8766, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN' })
  assert.equal(kernel.paths.root, path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'))
})

test('kernel paths derive from the plugin location unless configured', () => {
  const kernel = createKernel({ port: 8766, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', repoRoot: 'C:/tmp/doraemon-repo' })
  assert.equal(kernel.paths.root, 'C:\\tmp\\doraemon-repo')
  assert.ok(path.isAbsolute(kernel.paths.python), 'python path must resolve to an absolute file')
  assert.match(kernel.paths.python, /python\.exe$/i)
  // State and logs moved out of the kernel checkout: a published plugin must not
  // need write access to someone's repository.
  assert.equal(kernel.paths.state, join(homedir(), '.dsh', 'desktop'))
  assert.equal(kernel.paths.legacy, 'C:\\tmp\\doraemon-repo\\.dsh-desktop-local')
})

test('stateDir overrides where state lives, and logDir still works', () => {
  const base = { port: 8766, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', repoRoot: 'C:/tmp/doraemon-repo' }
  assert.equal(createKernel({ ...base, stateDir: 'custom' }).paths.state, 'C:\\tmp\\doraemon-repo\\custom')
  assert.equal(createKernel({ ...base, logDir: 'legacy-name' }).paths.state, 'C:\\tmp\\doraemon-repo\\legacy-name')
})

test('a kernel started by an older plugin is adopted through the legacy state file', async () => {
  const root = mkdtempSync(join(tmpdir(), 'doraemon-legacy-'))
  const token = 'legacy-bridge-token-used-by-an-older-plugin'
  mkdirSync(join(root, '.dsh-desktop-local'), { recursive: true })
  writeFileSync(join(root, '.dsh-desktop-local', 'kernel-state.json'), JSON.stringify({ port: 65010, token, pid: 4242 }))
  delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => (
    init?.headers?.Authorization === `Bearer ${token}`
      ? Response.json({ protocol: 1, execution_enabled: true })
      : new Response('{}', { status: 401 })
  )
  try {
    const kernel = createKernel({
      port: 65010, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', repoRoot: root,
      stateDir: 'state', autoStart: true, bootSeconds: 5,
    })
    const adopted = await kernel.ensure()
    assert.equal(adopted.own, false)
    assert.equal(adopted.source, 'legacy state file')
    // Lazy migration: the token is rewritten into the new location, so the
    // compatibility read is needed exactly once.
    const migrated = JSON.parse(readFileSync(join(kernel.paths.state, 'kernel-state.json'), 'utf8'))
    assert.equal(migrated.token, token)
    assert.equal(migrated.source, 'adopted-from-legacy')
  } finally {
    globalThis.fetch = original
    rmSync(root, { recursive: true, force: true })
  }
})

test('adopting a live kernel reconciles its PID without replacing state metadata', async () => {
  const root = mkdtempSync(join(tmpdir(), 'doraemon-adopt-pid-'))
  const token = 'test-adoption-token-01234567890123456789'
  const file = join(root, 'kernel-state.json')
  const oldToken = process.env.DSH_DESKTOP_BRIDGE_TOKEN
  const original = globalThis.fetch
  delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
  writeFileSync(file, JSON.stringify({ port: 65020, token, pid: 111, startedAt: 'original' }))
  globalThis.fetch = async () => Response.json({ protocol: 1, execution_enabled: true, bridge: { pid: 222 } })
  try {
    const kernel = createKernel({ port: 65020, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', stateDir: root, autoStart: false })
    assert.equal((await kernel.ensure()).pid, 222)
    assert.deepEqual(JSON.parse(readFileSync(file, 'utf8')), { port: 65020, token, pid: 222, startedAt: 'original' })
    kernel.observeHealth({ bridge: { pid: 333 } })
    assert.equal(JSON.parse(readFileSync(file, 'utf8')).pid, 333)
    writeFileSync(file, JSON.stringify({ port: 65020, token: 'different-token', pid: 444 }))
    kernel.observeHealth({ bridge: { pid: 555 } })
    assert.equal(JSON.parse(readFileSync(file, 'utf8')).pid, 444)
  } finally {
    globalThis.fetch = original
    if (oldToken === undefined) delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
    else process.env.DSH_DESKTOP_BRIDGE_TOKEN = oldToken
    rmSync(root, { recursive: true, force: true })
  }
})

test('a stale token no longer hides the working one', async () => {
  const root = mkdtempSync(join(tmpdir(), 'doraemon-stale-'))
  const good = 'working-bridge-token-for-this-test-0001'
  mkdirSync(join(root, 'state'), { recursive: true })
  writeFileSync(join(root, 'state', 'kernel-state.json'), JSON.stringify({ port: 65011, token: good }))
  process.env.DSH_DESKTOP_BRIDGE_TOKEN = 'stale-bridge-token-for-this-test-00002'
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => (
    init?.headers?.Authorization === `Bearer ${good}`
      ? Response.json({ protocol: 1, execution_enabled: true })
      : new Response('{}', { status: 401 })
  )
  try {
    const kernel = createKernel({
      port: 65011, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', repoRoot: root,
      stateDir: 'state', autoStart: true, bootSeconds: 5,
    })
    const adopted = await kernel.ensure()
    assert.equal(adopted.token, good)
  } finally {
    globalThis.fetch = original
    delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
    rmSync(root, { recursive: true, force: true })
  }
})

test('a kernel speaking an older protocol is refused with an upgrade hint', async () => {
  delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
  process.env.DSH_DESKTOP_BRIDGE_TOKEN = 'protocol-check-bridge-token-0123456789'
  const original = globalThis.fetch
  globalThis.fetch = async () => Response.json({ protocol: 0, execution_enabled: true })
  try {
    const kernel = createKernel({
      port: 65012, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', autoStart: true, bootSeconds: 5,
    })
    await assert.rejects(kernel.ensure(), (error) => {
      assert.match(error.message, /speaks protocol 0/)
      assert.match(error.message, /requires 1/)
      assert.match(error.message, /Update the DSH plugin package/)
      return true
    })
  } finally {
    globalThis.fetch = original
    delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
  }
})

test('kernel launches bundled source with the configured Python interpreter', () => {
  const dir = mkdtempSync(join(tmpdir(), 'desktop-python-'))
  const python = join(dir, 'python.exe')
  writeFileSync(python, '')
  try {
    const kernel = createKernel({ port: 65013, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', repoRoot: dir, pythonPath: python })
    const launch = kernel.executable()
    assert.equal(launch.source, 'config.pythonPath')
    assert.equal(launch.command, python)
    assert.deepEqual(launch.args, ['-m', 'dsh_desktop.dsh_bridge_cli'])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('stop refuses a stale or recycled kernel PID', async () => {
  const root = mkdtempSync(join(tmpdir(), 'dsh-stop-pid-'))
  const oldFetch = globalThis.fetch
  const oldKill = process.kill
  const oldToken = process.env.DSH_DESKTOP_BRIDGE_TOKEN
  delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
  const calls = []
  process.kill = (pid, signal) => { calls.push({ pid, signal }); return true }
  const storedToken = 'test-token-for-stale-pid-check-123456789'
  writeFileSync(join(root, 'kernel-state.json'), JSON.stringify({ port: 65019, token: storedToken, pid: 111 }))
  let reads = 0
  globalThis.fetch = async () => Response.json({ protocol: 1, execution_enabled: true, bridge: { pid: ++reads === 1 ? 111 : 222 } })
  try {
    const kernel = createKernel({ port: 65019, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', stateDir: root, autoStart: false })
    await kernel.ensure()
    assert.equal(await kernel.stop({ force: true }), false)
    assert.deepEqual(calls.map(x => x.signal), [0])
  } finally {
    globalThis.fetch = oldFetch
    process.kill = oldKill
    if (oldToken === undefined) delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
    else process.env.DSH_DESKTOP_BRIDGE_TOKEN = oldToken
    rmSync(root, { recursive: true, force: true })
  }
})

test('an explicit pythonPath that does not exist fails loudly', () => {
  const kernel = createKernel({
    port: 65014, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', repoRoot: 'C:/tmp/doraemon-repo',
    pythonPath: 'missing/python.exe',
  })
  assert.throws(() => kernel.executable(), /pythonPath points at/)
})

test('a POST route with no arguments is not silently sent as GET', async () => {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), method: init?.method, body: init?.body })
    return Response.json({ success: true })
  }
  process.env.DSH_DESKTOP_BRIDGE_TOKEN = 'method-shape-bridge-token-0123456789'
  try {
    const bridge = createBridge({ port: 65016, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', manageKernel: false }, fetch, {})
    await bridge.request('/windows/win-0123456789/capture', '0'.repeat(64), { method: 'POST' })
    await bridge.request('/parser/status', '0'.repeat(64), {})
    assert.deepEqual(calls.map((call) => call.method), ['POST', 'GET'])
    assert.equal(calls[0].body, '{}')
    assert.equal(calls[1].body, undefined)
  } finally {
    globalThis.fetch = original
    delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
  }
})

test('a missing endpoint tells the user to upgrade the kernel', async () => {
  process.env.DSH_DESKTOP_BRIDGE_TOKEN = 'http-hint-bridge-token-0123456789abcd'
  const original = globalThis.fetch
  globalThis.fetch = async () => new Response('{}', { status: 404 })
  try {
    const bridge = createBridge({ port: 65015, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', manageKernel: false }, fetch, {})
    await assert.rejects(bridge.request('/web/status', '0'.repeat(64), {}), /does not exist on the running kernel/)
  } finally {
    globalThis.fetch = original
    delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
  }
})

test('SSE frames split on blank lines and keep an incomplete tail', () => {
  const chunk = 'data: {"event":"bridge_status","data":{"status":"running"}}\n\n'
    + 'data: {"event":"step","data":{"text":"clicked"}}\n\n'
    + 'data: {"event":"bridge_status","data":{"status":"finished"}}'
  const { frames, rest } = extractFrames(chunk)
  assert.equal(frames.length, 2)
  assert.equal(frames[0].data.status, 'running')
  assert.equal(frames[1].event, 'step')
  assert.match(rest, /"finished"/)

  const done = extractFrames(rest + '\n\n')
  assert.equal(done.frames.length, 1)
  assert.equal(done.frames[0].data.status, 'finished')
  assert.equal(done.rest, '')
})

test('tool timeline pairs call/result and keeps analyze timings nested', () => {
  let now = 1000
  const timeline = createToolTimeline(() => now)
  timeline.observe({ event: 'tool_call', data: { call_id: 'a', name: 'ioa_analyze', args: { big: 'x'.repeat(500) } } })
  now += 150
  timeline.observe({
    event: 'tool_result',
    data: {
      call_id: 'a', name: 'ioa_analyze', duration_ms: 148,
      result: { success: true, effect: 'confirmed', timing_ms: { capture: 40, parse: 60, caption: 12, save: 3 } },
    },
  })
  timeline.observe({ event: 'reasoning', data: { text: 'x'.repeat(20000) } })
  timeline.observe({ event: 'tool_call', data: { call_id: 'b', name: 'ioa_click' } })
  now += 30
  timeline.observe({ event: 'tool_result', data: { call_id: 'b', name: 'ioa_click', result: { success: true } } })
  now += 5
  timeline.observe({ event: 'bridge_status', data: { status: 'finished' } })

  const summary = timeline.summary()
  assert.equal(summary.tool_calls, 2)
  assert.equal(summary.timeline[0].duration_ms, 148)
  assert.equal(summary.timeline[0].estimated, undefined)
  assert.deepEqual(summary.timeline[0].timing_ms, { capture: 40, parse: 60, caption: 12, save: 3 })
  // No duration_ms from the kernel: the arrival gap is used and flagged.
  assert.equal(summary.timeline[1].duration_ms, 30)
  assert.equal(summary.timeline[1].estimated, true)
  assert.equal(summary.total_tool_ms, 178)
  assert.deepEqual(summary.slowest.map((step) => step.name), ['ioa_analyze', 'ioa_click'])
  assert.equal(summary.task_ms, 185)
})

test('a refused action is not counted as a successful step', () => {
  const timeline = createToolTimeline(() => 0)
  timeline.observe({ event: 'tool_result', data: { call_id: 'x', name: 'ioa_click', duration_ms: 5, result: { effect: 'refused' } } })
  timeline.observe({ event: 'tool_result', data: { call_id: 'y', name: 'ioa_click', duration_ms: 5, result: { error: 'bad capture_id' } } })
  const summary = timeline.summary()
  assert.deepEqual(summary.timeline.map((step) => step.ok), [false, false])
})

test('a JSON-string tool result is parsed, not dumped as text', () => {
  const timeline = createToolTimeline(() => 0)
  timeline.observe({
    event: 'tool_result',
    data: { call_id: 'k', name: 'ioa_analyze', duration_ms: 9, result: '{"success":false,"effect":"refused"}' },
  })
  assert.equal(timeline.summary().timeline[0].ok, false)
})

test('a malformed SSE frame is skipped instead of killing the stream', () => {
  const { frames } = extractFrames('data: not-json\n\ndata: {"event":"ok"}\n\n')
  assert.equal(frames.length, 1)
  assert.equal(frames[0].event, 'ok')
})

test('web action resolves a semantic key through the knowledge base', async () => {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    const body = init?.body ? JSON.parse(init.body) : null
    calls.push({ url: String(url), body })
    if (String(url).endsWith('/kb/resolve')) {
      return Response.json({ success: true, found: true, value: 'button#go' })
    }
    if (String(url).endsWith('/kb/remember')) return Response.json({ success: true })
    return Response.json({ success: true, action: 'click', tool: 'web_click' })
  }
  try {
    const { tools } = setup()  // after the transport swap: createBridge captures fetch here
    const out = await tools.get('doraemon_web').execute({ action: 'click', key: '登录按钮' }, execution())
    assert.equal(out.success, true)
    assert.equal(calls[0].body.key, '登录按钮')
    assert.equal(calls[1].body.selector, 'button#go')
    assert.equal(out.kb_remembered, true)
  } finally {
    globalThis.fetch = original
  }
})

test('web action refuses an unknown action and a missing selector', async () => {
  const { tools } = setup()
  await assert.rejects(tools.get('doraemon_web').execute({ action: 'eval_js' }, execution()), /action must be one of/)
  await assert.rejects(tools.get('doraemon_web').execute({ action: 'click' }, execution()), /needs selector/)
})

test('package.json is BOM-free and parseable, and every shipped file exists', async () => {
  // A UTF-8 BOM written by an editor once broke a DSH profile's package.json;
  // the shipped manifest must never carry one.
  const raw = await readFile(new URL('../package.json', import.meta.url))
  assert.notDeepEqual([...raw.subarray(0, 3)], [0xef, 0xbb, 0xbf], 'package.json must not start with a BOM')
  const manifest = JSON.parse(raw.toString('utf8'))
  for (const file of manifest.files.filter(file => !file.includes('*') && !file.startsWith('private-knowledge/'))) {
    await readFile(new URL(`../${file}`, import.meta.url))
  }
  assert.ok(manifest.files.includes('kernel/**/*'), 'bundled kernel source must be included')
  assert.ok(manifest.files.includes('requirements.txt'), 'runtime requirements must be included')
  assert.ok(manifest.files.includes('vendor/weights/**/*'), 'bundled model weights must be included')
  assert.ok(manifest.files.includes('README.md'), 'README.md must ship with the package')
})

test('DSH_DESKTOP_PORT overrides the single default port', async () => {
  const original = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url) => {
    calls.push(String(url))
    return Response.json({ success: true })
  }
  process.env.DSH_DESKTOP_PORT = '8899'
  try {
    const { tools } = setup()
    await tools.get('doraemon_click').execute({ window_id: 'win-0123456789', x: 10, y: 10 }, execution())
    assert.match(calls[0], /^http:\/\/127\.0\.0\.1:8899\//)
  } finally {
    delete process.env.DSH_DESKTOP_PORT
    globalThis.fetch = original
  }
})

test('an invalid DSH_DESKTOP_PORT fails loudly instead of dialling a random port', () => {
  process.env.DSH_DESKTOP_PORT = '99999'
  try {
    assert.throws(() => setup(), /DSH_DESKTOP_PORT must be an integer between 1024 and 65535/)
  } finally {
    delete process.env.DSH_DESKTOP_PORT
  }
})

test('a non-Windows host gets one clear error when the kernel must be started', async () => {
  // An unused port keeps this hermetic: nothing answers, so boot() reaches the
  // spawn path instead of adopting a kernel that happens to run on this machine.
  const kernel = createKernel(
    { port: 65001, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', autoStart: true, bootSeconds: 5 },
    { platform: 'darwin' },
  )
  await assert.rejects(kernel.ensure(), /requires Windows/)
})

test('a non-Windows host can adopt a kernel that already answers', async () => {
  // The narrowed guard: platform only matters when we would spawn. A kernel that
  // is already healthy on loopback is adopted, so no Windows error is raised.
  const original = globalThis.fetch
  process.env.DSH_DESKTOP_BRIDGE_TOKEN = 'adopt-only-bridge-token-for-this-test'
  globalThis.fetch = async () => Response.json({ protocol: 1, execution_enabled: true })
  try {
    const kernel = createKernel(
      { port: 65003, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', autoStart: true, bootSeconds: 5 },
      { platform: 'darwin' },
    )
    const adopted = await kernel.ensure()
    assert.equal(adopted.own, false)
    assert.equal(adopted.source, 'environment')
  } finally {
    globalThis.fetch = original
    delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
  }
})

test('a non-Windows host still works as a plain HTTP client', async () => {
  // manageKernel=false never calls ensure(), so the platform guard must not fire;
  // the only complaint left is the missing token.
  delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
  const bridge = createBridge(
    { port: 65002, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', manageKernel: false },
    fetch,
    { platform: 'darwin' },
  )
  await assert.rejects(bridge.request('/health', '0'.repeat(64), {}), /Set DSH_DESKTOP_BRIDGE_TOKEN/)
})

test('click with a goal lets the local decision layer pick the element', async () => {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    const body = init?.body ? JSON.parse(init.body) : null
    calls.push({ url: String(url), body })
    if (String(url).endsWith('/decide')) {
      return Response.json({
        success: true, id: 3, score: 0.91, confidence: 0.5, source: 'uia', table_size: 118,
        reason: 'click-fit',
        picked: { id: 3, text: '资料库', type: 'TabItem', bbox: [9, 318, 189, 356] },
        candidates: [],
      })
    }
    return Response.json({ success: true, effect: 'confirmed', dispatch_used: 'uia_select' })
  }
  try {
    const { tools } = setup()
    const out = await tools.get('doraemon_click').execute(
      { window_id: 'win-0123456789', goal: '点击资料库' }, execution())
    assert.equal(out.success, true)
    assert.equal(out.decided.text, '资料库')
    assert.equal(calls[0].body.goal, '点击资料库')
    // bbox centre, normalized: ((9+189)/2, (318+356)/2)
    assert.equal(calls[1].body.x, 99)
    assert.equal(calls[1].body.y, 337)
    assert.equal(calls[1].body.name, '资料库')
  } finally {
    globalThis.fetch = original
  }
})

test('a goal below the decision threshold clicks nothing', async () => {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    calls.push(String(url))
    return Response.json({ success: false, score: 0.21, reason: 'below threshold', candidates: [] })
  }
  try {
    const { tools } = setup()
    const out = await tools.get('doraemon_click').execute(
      { window_id: 'win-0123456789', goal: '点击根本不存在的东西' }, execution())
    assert.equal(out.success, false)
    assert.equal(out.effect, 'refused')
    assert.equal(calls.length, 1)  // decide only; no click was attempted
  } finally {
    globalThis.fetch = original
  }
})

test('window_wait_ms is forwarded so a spawned window is caught', async () => {
  const bodies = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    bodies.push(JSON.parse(init.body))
    return Response.json({ success: true, effect: 'confirmed', window_evidence: 'new_window' })
  }
  try {
    const { tools } = setup()
    await tools.get('doraemon_click').execute(
      { window_id: 'win-0123456789', x: 100, y: 200, dispatch: 'auto', window_wait_ms: 8000 },
      execution())
    assert.equal(bodies[0].window_wait_ms, 8000)
  } finally {
    globalThis.fetch = original
  }
})

test('doraemon_wait_window returns the spawned window', async () => {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), body: JSON.parse(init.body) })
    return Response.json({
      success: true, waited_ms: 7300,
      added: [{ hwnd: 8787372, exe: 'ztsmsysoptimize.exe', title: '清理垃圾' }],
    })
  }
  try {
    const { tools } = setup()
    const out = await tools.get('doraemon_wait_window').execute(
      { window_id: 'win-0123456789', timeout_ms: 12000 }, execution())
    assert.equal(out.success, true)
    assert.equal(calls[0].body.timeout_ms, 12000)
    assert.match(calls[0].url, /\/windows\/win-0123456789\/wait-window$/)
    assert.equal(out.added[0].exe, 'ztsmsysoptimize.exe')
  } finally {
    globalThis.fetch = original
  }
})

test('web dom action queries components by CSS selector', async () => {
  const bodies = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    bodies.push(JSON.parse(init.body))
    return Response.json({
      success: true, action: 'dom', count: 2,
      elements: [
        { tag: 'div', cls: ['el-dropdown', 'is-expanded'], text: '根域', visible: true, rect: [8, 100, 184, 36] },
        { tag: 'button', cls: ['el-button'], text: '确定', visible: true, rect: [8, 200, 88, 32] },
      ],
    })
  }
  try {
    const { tools } = setup()
    const out = await tools.get('doraemon_web').execute(
      { action: 'dom', selector: '[class*="el-button"]', max_elements: 50 }, execution())
    assert.equal(out.success, true)
    assert.equal(out.count, 2)
    assert.equal(bodies[0].action, 'dom')
    assert.equal(bodies[0].selector, '[class*="el-button"]')
    assert.equal(bodies[0].max_elements, 50)
  } finally {
    globalThis.fetch = original
  }
})


test('a write action acquires the lease automatically for this session', async () => {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    const body = init?.body ? JSON.parse(init.body) : null
    calls.push(body)
    if (body?.action === 'lease') return Response.json({ success: true, held: true, owner: body.owner })
    return Response.json({ success: true, action: 'click', tool: 'web_click' })
  }
  try {
    const { tools } = setup()
    const out = await tools.get('doraemon_web').execute(
      { action: 'click', selector: '#go', allow_mutating: true, verify: '列表出现新行' },
      execution(),
    )
    assert.equal(out.success, true)
    const lease = calls.find((c) => c?.action === 'lease')
    assert.ok(lease, 'a write must acquire the lease first')
    assert.equal(lease.op, 'acquire')
    const click = calls.find((c) => c?.action === 'click')
    assert.equal(click.owner, lease.owner, 'the click must carry the same owner')
  } finally {
    globalThis.fetch = original
  }
})

test('a write is refused (not silently run) when another session holds the lease', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    const body = init?.body ? JSON.parse(init.body) : null
    if (body?.action === 'lease') {
      return Response.json({ success: false, held: false, holder: 'other', expires_in_s: 42 })
    }
    throw new Error('the write must not reach the kernel while the lease is held')
  }
  try {
    const { tools } = setup()
    const out = await tools.get('doraemon_web').execute(
      { action: 'click', selector: '#go', allow_mutating: true, verify: 'x' },
      execution(),
    )
    assert.equal(out.success, false)
    assert.equal(out.lease_conflict, true)
    assert.equal(out.holder, 'other')
  } finally {
    globalThis.fetch = original
  }
})

test('read actions do not touch the lease', async () => {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    calls.push(init?.body ? JSON.parse(init.body) : null)
    return Response.json({ success: true, action: 'snapshot', tool: 'web_snapshot' })
  }
  try {
    const { tools } = setup()
    await tools.get('doraemon_web').execute({ action: 'snapshot' }, execution())
    assert.equal(calls.some((c) => c?.action === 'lease'), false)
  } finally {
    globalThis.fetch = original
  }
})

test('action=lease forwards op/ttl_s/force', async () => {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    const body = init?.body ? JSON.parse(init.body) : null
    calls.push(body)
    return Response.json({ success: true, held: true })
  }
  try {
    const { tools } = setup()
    await tools.get('doraemon_web').execute(
      { action: 'lease', op: 'release', ttl_s: 60, force: true }, execution())
    assert.equal(calls[0].action, 'lease')
    assert.equal(calls[0].op, 'release')
    assert.equal(calls[0].ttl_s, 60)
    assert.equal(calls[0].force, true)
  } finally {
    globalThis.fetch = original
  }
})

test('commit=local is forwarded to the kernel', async () => {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    const body = init?.body ? JSON.parse(init.body) : null
    calls.push(body)
    if (body?.action === 'lease') return Response.json({ success: true, held: true })
    return Response.json({ success: true })
  }
  try {
    const { tools } = setup()
    await tools.get('doraemon_web').execute(
      { action: 'click', selector: '#ok', allow_mutating: true, commit: 'local', verify: '表单出现该用户' },
      execution(),
    )
    const click = calls.find((c) => c?.action === 'click')
    assert.equal(click.commit, 'local')
  } finally {
    globalThis.fetch = original
  }
})

test('a write releases the lease in finally (no stale lease after the call)', async () => {
  const ops = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    const body = init?.body ? JSON.parse(init.body) : null
    if (body?.action === 'lease') {
      ops.push(body.op)
      return Response.json({ success: true, held: true, owner: body.owner })
    }
    ops.push(body?.action)
    return Response.json({ success: true })
  }
  try {
    const { tools } = setup()
    await tools.get('doraemon_web').execute(
      { action: 'click', selector: '#go', allow_mutating: true, verify: 'x' }, execution())
    assert.deepEqual(ops, ['acquire', 'click', 'release'])
  } finally {
    globalThis.fetch = original
  }
})

test('the lease is released even when the action throws', async () => {
  const ops = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    const body = init?.body ? JSON.parse(init.body) : null
    if (body?.action === 'lease') {
      ops.push(body.op)
      return Response.json({ success: true, held: true, owner: body.owner })
    }
    ops.push('boom')
    throw new Error('kernel exploded')
  }
  try {
    const { tools } = setup()
    await assert.rejects(tools.get('doraemon_web').execute(
      { action: 'click', selector: '#go', allow_mutating: true, verify: 'x' }, execution()))
    assert.deepEqual(ops, ['acquire', 'boom', 'release'],
      'a crash must not leave the lease held')
  } finally {
    globalThis.fetch = original
  }
})

test('a read action never touches the lease', async () => {
  const ops = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    const body = init?.body ? JSON.parse(init.body) : null
    ops.push(body?.action)
    return Response.json({ success: true })
  }
  try {
    const { tools } = setup()
    await tools.get('doraemon_web').execute({ action: 'snapshot' }, execution())
    assert.deepEqual(ops, ['snapshot'])
  } finally {
    globalThis.fetch = original
  }
})
test('page cache forwards advisory lookup and opt-in navigation without retrying', async () => {
  const original = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return new Response('{"success":true,"cache_hit":false}')
  }
  try {
    const { tools } = setup({ enableExperimentalPageCache: true })
    await tools.get('doraemon_page_cache').execute({
      window_id: 'win-0123456789', op: 'navigate', target: 'Tools',
      control_type: 'TabItem', expected: 'Tool page', method: 'click', enabled: false,
    }, execution())
    assert.equal(calls.length, 1)
    assert.match(calls[0].url, /\/page-cache$/)
    assert.deepEqual(JSON.parse(calls[0].options.body), {
      op: 'navigate', target: 'Tools', control_type: 'TabItem', expected: 'Tool page', method: 'click', enabled: false,
    })
  } finally { globalThis.fetch = original }
})


test('experimental page cache is disabled by default and needs explicit enabling', async () => {
  const normal = setup()
  assert.equal(normal.tools.has('doraemon_page_cache'), false)
  const original = globalThis.fetch
  let body
  globalThis.fetch = async (_url, options) => { body = JSON.parse(options.body); return new Response('{"success":true}') }
  try {
    const experimental = setup({ enableExperimentalPageCache: true })
    assert.equal(experimental.tools.has('doraemon_page_cache'), true)
    await experimental.tools.get('doraemon_page_cache').execute({ window_id: 'win-0123456789', op: 'lookup', target: 'Tools', control_type: 'TabItem', expected: 'Tool page' }, execution())
    assert.equal(body.enabled, false)
  } finally { globalThis.fetch = original }
})

test('bootstrap installation is discovered without changing PATH or a profile', () => {
  const root = mkdtempSync(join(tmpdir(), 'doraemon-bootstrap-'))
  const oldLocal = process.env.LOCALAPPDATA
  const oldRepo = process.env.DSH_DESKTOP_REPO
  try {
    process.env.LOCALAPPDATA = root
    delete process.env.DSH_DESKTOP_REPO
    const python = join(root, 'dsh-desktop', 'venv', 'Scripts', 'python.exe')
    mkdirSync(path.dirname(python), { recursive: true })
    writeFileSync(python, '')
    const kernel = createKernel({ port: 65018, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN' })
    assert.equal(kernel.executable().command, python)
    assert.equal(kernel.executable().source, 'bootstrap venv')
    assert.equal(kernel.paths.home, join(root, 'dsh-desktop'))
  } finally {
    if (oldLocal === undefined) delete process.env.LOCALAPPDATA
    else process.env.LOCALAPPDATA = oldLocal
    if (oldRepo === undefined) delete process.env.DSH_DESKTOP_REPO
    else process.env.DSH_DESKTOP_REPO = oldRepo
    rmSync(root, { recursive: true, force: true })
  }
})
