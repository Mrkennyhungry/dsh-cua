import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { syncBuiltinESMExports } from 'node:module'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { createToolRegistry, createToolTimeline } from '../tool-registry.js'

const taskId = 'a'.repeat(32)
const identity = tool => tool
const ownerOf = exec => exec.owner
const originalTools = [
  'doraemon_run', 'doraemon_status', 'doraemon_stop', 'doraemon_steer',
  'doraemon_windows', 'doraemon_analyze', 'doraemon_uia', 'doraemon_uia_batch',
  'doraemon_wait_window', 'doraemon_click', 'doraemon_type', 'doraemon_key',
  'doraemon_scroll', 'doraemon_swipe', 'doraemon_apps', 'doraemon_drag',
  'doraemon_clipboard', 'doraemon_click_pixel', 'doraemon_topmost', 'doraemon_cleanup',
  'doraemon_parser', 'doraemon_files', 'doraemon_invoke', 'doraemon_capture',
  'doraemon_wait_stable', 'doraemon_cursor', 'doraemon_highlight', 'doraemon_web',
  'doraemon_kb', 'doraemon_health',
]

function execution(owner = 'adapter-owner') {
  return { owner, callId: 'call-1', signal: new AbortController().signal }
}

function setup(options = {}, overrides = {}, defineTool = identity) {
  const calls = [], waits = [], streams = [], shutdowns = []
  const bridge = {
    ready: true,
    taskPath: (id, suffix = '') => `/tasks/${id}${suffix}`,
    async request(path, owner, options = {}) {
      options.signal?.throwIfAborted()
      calls.push({ path, owner, options })
      if (path === '/tasks') return { task_id: taskId, reused: true }
      if (path === `/tasks/${taskId}`) return { task_id: taskId, status: 'running' }
      return { success: true }
    },
    async wait(id, owner, signal, seconds) {
      waits.push({ id, owner, signal, seconds })
      return { task_id: id, status: 'running' }
    },
    async stream(path, owner, options) { streams.push({ path, owner, options }) },
    async shutdown(force) { shutdowns.push(force) },
    ...overrides,
  }
  const config = { waitSeconds: 10, keepAlive: true, enableExperimentalPageCache: false, ...options }
  const registry = createToolRegistry(config, { bridge, defineTool, ownerOf })
  return { ...registry, bridge, calls, waits, streams, shutdowns,
    byName: new Map(registry.tools.map(tool => [tool.name, tool])) }
}

function mockCatalog(t, content) {
  const catalogPath = fileURLToPath(new URL('../private-knowledge/console-components.json', import.meta.url))
  const existsSync = fs.existsSync, readFileSync = fs.readFileSync
  t.mock.method(fs, 'existsSync', path => path === catalogPath || existsSync(path))
  t.mock.method(fs, 'readFileSync', (path, ...args) => path === catalogPath ? content : readFileSync(path, ...args))
  syncBuiltinESMExports()
  t.after(() => { t.mock.restoreAll(); syncBuiltinESMExports() })
  return catalogPath
}

test('registry 无 DSH 包也能独立导入', () => {
  const url = new URL('../tool-registry.js', import.meta.url).href
  const script = `
    import { registerHooks } from 'node:module';
    registerHooks({ resolve(specifier, context, nextResolve) {
      if (specifier.startsWith('@deepseek-ai/')) throw new Error('不得加载 DSH 包');
      return nextResolve(specifier, context);
    } });
    const registry = await import(${JSON.stringify(url)});
    if (typeof registry.createToolRegistry !== 'function' || typeof registry.createToolTimeline !== 'function') process.exit(1);
  `
  const child = spawnSync(process.execPath, ['--input-type=module', '-e', script], { encoding: 'utf8' })
  assert.ifError(child.error)
  assert.equal(child.status, 0, child.stderr)
})

test('identity defineTool 保留 30 项原有工具及源码已有的屏幕点击工具', () => {
  const { tools, calls, waits, streams, shutdowns, dispose } = setup()
  const names = tools.map(tool => tool.name)
  assert.equal(originalTools.length, 30)
  assert.deepEqual(names.filter(name => name !== 'doraemon_click_screen' && name !== 'doraemon_product_knowledge'), originalTools)
  assert.ok(names.includes('doraemon_click_screen'))
  assert.equal(new Set(names).size, names.length)
  assert.equal(names.filter(name => name !== 'doraemon_product_knowledge').length, 31)
  assert.equal(typeof dispose, 'function')
  for (const tool of tools) {
    assert.equal(typeof tool.description, 'string')
    assert.equal(typeof tool.execute, 'function')
    assert.equal(tool.output.schema.type, 'object')
    assert.deepEqual(tool.output.render({}, { success: true }), [{ type: 'text', text: '{"success":true}' }])
  }
  assert.deepEqual([calls, waits, streams, shutdowns], [[], [], [], []])
})

test('tools 使用注入 defineTool 的返回值', () => {
  const descriptors = []
  const { tools } = setup({}, {}, descriptor => {
    descriptors.push(descriptor)
    return { descriptor }
  })
  assert.equal(tools.length, descriptors.length)
  tools.forEach((tool, i) => assert.equal(tool.descriptor, descriptors[i]))
})

test('工具通过注入 ownerOf 和 fake bridge 保留参数归一化及拒绝行为', async () => {
  const { byName, calls } = setup()
  const exec = execution('non-dsh-owner')
  await byName.get('doraemon_click').execute({ window_id: 'win-0123456789', x: 120, y: 340 }, exec)
  assert.deepEqual(calls, [{ path: '/windows/win-0123456789/click', owner: 'non-dsh-owner',
    options: { body: { x: 120, y: 340, clicks: 1, button: 'left' }, signal: exec.signal } }])
  await assert.rejects(byName.get('doraemon_click').execute({ window_id: 'win-0123456789', x: 1001, y: 0 }, exec), /0–1000/)
  assert.equal(calls.length, 1)
})

test('follow=false 保持既有 bridge.wait 行为而非立即返回', async () => {
  const { byName, calls, waits, streams } = setup({ waitSeconds: 17 })
  const exec = execution()
  const result = await byName.get('doraemon_run').execute({ task: '仅模拟授权任务', follow: false }, exec)
  assert.deepEqual(calls[0], { path: '/tasks', owner: exec.owner,
    options: { body: { task: '仅模拟授权任务', request_id: exec.callId }, signal: exec.signal } })
  assert.deepEqual(waits, [{ id: taskId, owner: exec.owner, signal: exec.signal, seconds: 17 }])
  assert.deepEqual(streams, [])
  assert.deepEqual(result, { task_id: taskId, status: 'running', request_id: exec.callId, reused: true })
})

test('follow 保留 SSE timeline 和最近事件，已结束任务不再由 dispose 停止', async () => {
  const events = [
    { event: 'tool_call', data: { call_id: 'one', name: 'analyze' } },
    { event: 'reasoning', data: { text: '不可展示的噪声' } },
    { event: 'tool_result', data: { call_id: 'one', name: 'analyze', duration_ms: 9,
      result: { success: true, timing_ms: { capture: 3 } } } },
    { event: 'bridge_status', data: { task_id: taskId, status: 'finished', result: { task_outcome: 'unknown' } } },
  ]
  const state = setup({}, { async stream(path, owner, options) {
    assert.equal(path, `/tasks/${taskId}/events`)
    assert.equal(owner, 'adapter-owner')
    assert.equal(options.timeoutMs, 20000)
    events.forEach(options.onEvent)
  } })
  const result = await state.byName.get('doraemon_run').execute({ task: '仅模拟授权任务' }, execution())
  assert.equal(result.status, 'finished')
  assert.equal(result.result.task_outcome, 'unknown')
  assert.equal(result.events, 4)
  assert.equal(result.tool_calls, 1)
  assert.equal(result.total_tool_ms, 9)
  assert.deepEqual(result.timeline[0].timing_ms, { capture: 3 })
  assert.equal(result.recent_events.some(event => event.type === 'reasoning'), false)
  await state.dispose()
  assert.deepEqual(state.calls.map(call => call.path), ['/tasks'])
})

test('dispose 停止活跃任务，不使用调用 signal，keepAlive=true 不关机', async () => {
  const { byName, dispose, calls, shutdowns } = setup()
  const exec = execution()
  await byName.get('doraemon_run').execute({ task: '仅模拟授权任务', follow: false }, exec)
  await dispose()
  assert.deepEqual(calls.at(-1), { path: `/tasks/${taskId}/stop`, owner: exec.owner, options: { body: {} } })
  assert.deepEqual(shutdowns, [])
  const count = calls.length
  await dispose()
  assert.equal(calls.length, count)
})

test('dispose 在 bridge 未 ready 时不请求 stop，但 keepAlive=false 仍关机并清空活跃任务', async () => {
  const state = setup({ keepAlive: false })
  await state.byName.get('doraemon_run').execute({ task: '仅模拟授权任务', follow: false }, execution())
  state.bridge.ready = false
  await state.dispose()
  assert.deepEqual(state.calls.map(call => call.path), ['/tasks'])
  assert.deepEqual(state.shutdowns, [true])
  state.bridge.ready = true
  await state.dispose()
  assert.deepEqual(state.calls.map(call => call.path), ['/tasks'])
})

test('dispose 使用 allSettled，stop 失败仍清空任务并关机', async () => {
  const state = setup({ keepAlive: false })
  await state.byName.get('doraemon_run').execute({ task: '仅模拟授权任务', follow: false }, execution())
  const request = state.bridge.request
  let stops = 0
  state.bridge.request = async (path, ...args) => {
    if (path.endsWith('/stop')) { stops += 1; throw new Error('模拟 stop 失败') }
    return request(path, ...args)
  }
  await state.dispose()
  assert.equal(stops, 1)
  assert.deepEqual(state.shutdowns, [true])
  await state.dispose()
  assert.equal(stops, 1)
})

test('实验 page cache 仍需显式启用，省略 enabled 时仍为 false', async () => {
  assert.equal(setup().byName.has('doraemon_page_cache'), false)
  const { byName, calls } = setup({ enableExperimentalPageCache: true })
  await byName.get('doraemon_page_cache').execute({ window_id: 'win-0123456789', op: 'lookup',
    target: 'Tools', control_type: 'TabItem', expected: 'Tool page' }, execution())
  assert.equal(calls[0].path, '/windows/win-0123456789/page-cache')
  assert.equal(calls[0].options.body.enabled, false)
})

test('产品 catalog 仍从根目录 private-knowledge 加载', t => {
  const entry = { family: 'test-panel', description: '模拟组件', aliases: ['test-label'], routes: [], classes: [] }
  const catalogPath = mockCatalog(t, JSON.stringify({ entries: [entry] }))
  const { byName } = setup()
  const tool = byName.get('doraemon_product_knowledge')
  assert.ok(tool)
  assert.deepEqual(tool.execute({ query: 'test-label' }), { success: true, matches: [{ ...entry, score: 1 }] })
  assert.equal(fs.existsSync(catalogPath), true)
})

test('损坏的可选 catalog 保留警告日志，仍注册默认工具', t => {
  mockCatalog(t, 'not-json')
  const warn = t.mock.method(console, 'warn', () => {})
  const { byName } = setup()
  assert.equal(byName.has('doraemon_product_knowledge'), false)
  assert.equal(warn.mock.callCount(), 1)
  assert.equal(warn.mock.calls[0].arguments[0], '[doraemon] optional product component catalog could not be loaded: %s')
})

test('独立导出的 timeline 保留拒绝判定和嵌套耗时', () => {
  let now = 1000
  const timeline = createToolTimeline(() => now)
  timeline.observe({ event: 'tool_call', data: { call_id: 'one', name: 'analyze' } })
  now += 7
  timeline.observe({ event: 'tool_result', data: { call_id: 'one', name: 'analyze',
    result: '{"effect":"refused","timing_ms":{"capture":3}}' } })
  now += 2
  timeline.observe({ event: 'bridge_status', data: { status: 'finished' } })
  assert.deepEqual(timeline.summary().timeline, [{ name: 'analyze', duration_ms: 7, ok: false,
    estimated: true, timing_ms: { capture: 3 }, effect: 'refused' }])
  assert.equal(timeline.summary().task_ms, 9)
})
