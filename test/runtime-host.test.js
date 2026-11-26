import test from 'node:test'
import assert from 'node:assert/strict'
import { ToolExecutionPipeline } from '@deepseek-ai/dsh-tools/pipeline'
import { registerRuntimeTools } from '../runtime-host.js'
import { apply, Config } from '../index.js'

function fixture() {
  const tools = new Map(), calls = []
  const attachment = { attachmentId: 'image-1', mediaType: 'image/jpeg', bytes: 3, width: 100, height: 50 }
  const ctx = { get: name => name === 'attachments' ? { saveImage: async input => {
    assert.deepEqual(input.data, new Uint8Array([1, 2, 3]))
    assert.equal(input.mediaType, 'image/jpeg')
    return attachment
  } } : undefined }
  const bridge = { async request(path, owner, options) {
    calls.push({ path, owner, ...options })
    if (options.image) return new Uint8Array([1, 2, 3])
    if (path === '/runtime/observe') return { success: true, observation: { frames: [{ frame_id: 'f_1', width: 100, height: 50 }] } }
    return { success: true, status: 'completed' }
  } }
  registerRuntimeTools(ctx, bridge, tool => tools.set(tool.name, tool))
  return { tools, calls, attachment }
}

test('the default plugin entry registers only the six runtime tools', async () => {
  const tools = new Map(), effects = []
  apply({ tools: { register: tool => tools.set(tool.name, tool) }, effect: effect => effects.push(effect) },
    Config({ manageKernel: true, commandFeedback: false }))
  assert.deepEqual([...tools.keys()], ['doraemon_open', 'doraemon_observe', 'doraemon_act', 'doraemon_wait', 'doraemon_status', 'doraemon_stop'])
  for (const effect of effects) await effect()?.()
})

test('runtime adapter registers six tools and forwards operation identity and cancellation', async () => {
  const { tools, calls } = fixture()
  assert.deepEqual([...tools.keys()], ['doraemon_open', 'doraemon_observe', 'doraemon_act', 'doraemon_wait', 'doraemon_status', 'doraemon_stop'])
  const signal = new AbortController().signal
  const exec = { callId: 'call-1', signal, agent: { session: { id: 'session-1' } } }
  assert.match(tools.get('doraemon_open').parameters.properties.args.description, /absolute path/)
  assert.equal(tools.get('doraemon_open').parameters.properties.hwnd.type, 'integer')
  assert.equal(tools.get('doraemon_observe').parameters.properties.include_untitled.type, 'boolean')
  assert.match(tools.get('doraemon_observe').parameters.properties.query.description, /Literal, case-insensitive substring/)
  assert.match(tools.get('doraemon_act').parameters.properties.steps.items.properties.keys.description, /\["CTRL", "S"\]/)
  assert.equal(tools.get('doraemon_status').parameters.properties.request_key.type, 'string')
  assert.ok(tools.get('doraemon_wait').parameters.properties.condition.properties.kind.enum.includes('transient_watch'))
  assert.ok(!tools.get('doraemon_act').parameters.properties.expect.properties.kind.enum.includes('transient_watch'))
  assert.deepEqual(tools.get('doraemon_wait').parameters.properties.condition.properties.state.enum,
    ['ready', 'closed', 'url_changed', 'visible', 'hidden', 'enabled', 'disabled', 'checked', 'unchecked', 'focused'])
  await tools.get('doraemon_open').execute({ url: 'https://example.test' }, exec)
  assert.deepEqual(calls[0].body, { request_key: 'call-1', arguments: { url: 'https://example.test' } })
  assert.equal(calls[0].signal, signal)
  assert.match(calls[0].owner, /^[a-f0-9]{64}$/)
})

test('runtime observation emits an actual DSH image attachment through the tool pipeline', async () => {
  const { tools, calls, attachment } = fixture()
  const pipeline = new ToolExecutionPipeline({
    tools: { checkGuards: () => undefined },
    waterfall: (_subject, _event, ...args) => args.at(-1)(),
    events: { dispatch: () => [] },
    logger: { warn: message => assert.fail(message) },
  }, { get: name => tools.get(name) })
  const signal = new AbortController().signal
  const prepared = await pipeline.prepare({ name: 'doraemon_observe', arguments: { image: true },
    callId: 'observe-call', signal, agent: { session: { id: 'session-1' } } })
  const outcome = await pipeline.dispatch(prepared.exec)
  const result = await pipeline.complete(prepared.exec, outcome)
  assert.equal(result.isError, false)
  assert.deepEqual(result.content[1], { type: 'image', attachment })
  assert.equal(calls[1].path, '/runtime/frames/f_1')
  assert.equal(calls[1].signal, signal)
  const invalid = await pipeline.prepare({ name: 'doraemon_wait',
    arguments: { condition: { kind: 'state', state: 'browser_closed' } }, callId: 'invalid-state', signal,
    agent: { session: { id: 'session-1' } } })
  const rejected = await pipeline.complete(invalid.exec, await pipeline.dispatch(invalid.exec))
  assert.equal(rejected.isError, true)
  assert.equal(calls.length, 2)
  const valid = await pipeline.prepare({ name: 'doraemon_wait',
    arguments: { condition: { kind: 'state', state: 'closed' } }, callId: 'valid-state', signal,
    agent: { session: { id: 'session-1' } } })
  const accepted = await pipeline.complete(valid.exec, await pipeline.dispatch(valid.exec))
  assert.equal(accepted.isError, false)
})

test('an image storage failure preserves dispatched operation data without unusable coordinates', async () => {
  const tools = new Map()
  const bridge = { async request(path) {
    if (path.includes('/frames/')) return new Uint8Array([1])
    return { success: true, operation_id: 'op-1', status: 'completed', completed_steps: [0],
      post_click_frame: { frame_id: 'f_2', width: 100, height: 50 },
      popup_candidates: [{ ref: 'p1', bounds: { frame_id: 'f_2', x: 2, y: 3 } }],
      popup_region: { frame_id: 'f_2', x: 2, y: 3 }, local_sample_region: { frame_id: 'f_2', x: 0, y: 0 },
      click_region_frames: { before: { frame_id: 'f_3' }, after: { frame_id: 'f_4' } },
      observation: { frames: [{ frame_id: 'f_1', width: 100, height: 50 }],
        elements: [{ ref: 'e1', bounds: { frame_id: 'f_1', x: 1, y: 2 } }] } }
  } }
  registerRuntimeTools({ get: () => ({ saveImage: async () => { throw new Error('storage failed') } }) }, bridge,
    tool => tools.set(tool.name, tool))
  const exec = { callId: 'call-1', signal: new AbortController().signal, agent: { session: { id: 'session-1' } } }
  const result = await tools.get('doraemon_act').execute({ target_id: 't', observation_id: 'o', steps: [{ action: 'close' }] }, exec)
  assert.equal(result.success, true)
  assert.equal(result.image_warning.code, 'IMAGE_ATTACHMENT_FAILED')
  assert.equal(result.operation_id, 'op-1')
  assert.deepEqual(result.completed_steps, [0])
  assert.deepEqual(result.observation.frames, [])
  assert.deepEqual(result.observation.elements, [{ ref: 'e1' }])
  assert.equal(result.post_click_frame, undefined)
  assert.equal(result.popup_candidates, undefined)
  assert.equal(result.popup_region, undefined)
  assert.equal(result.local_sample_region, undefined)
  assert.equal(result.click_region_frames, undefined)
  assert.match(result.next_action, /Observe the target again/)
})

test('click evidence attaches full readback and immediate frame followed by before and after crops', async () => {
  const tools = new Map(), saved = []
  const frames = ['readback', 'immediate', 'before', 'after'].map(frame_id => ({ frame_id, width: 400, height: 300 }))
  registerRuntimeTools({ get: () => ({ saveImage: async input => {
    saved.push(input.name)
    return { attachmentId: input.name, width: 400, height: 300 }
  } }) }, { async request(path, _owner, options) {
    if (options.image) return new Uint8Array([1])
    return { success: true, observation: { frames: [frames[0]] }, post_click_frame: frames[1],
      click_region_frames: { before: frames[2], after: frames[3] } }
  } }, tool => tools.set(tool.name, tool))
  const result = await tools.get('doraemon_act').execute({ target_id: 't', observation_id: 'o', steps: [{ action: 'close' }] }, {
    callId: 'click', signal: new AbortController().signal, agent: { session: { id: 'a' } },
  })
  assert.deepEqual(saved, ['readback.jpg', 'immediate.jpg', 'before.jpg', 'after.jpg'])
  assert.equal(result.attachments.length, 4)
})

test('a lost operation response is reconciled by request key without redispatch', async () => {
  const tools = new Map(), calls = []
  const signal = new AbortController().signal
  registerRuntimeTools({}, { async request(path, owner, options) {
    calls.push({ path, owner, ...options })
    if (path === '/runtime/act') throw new TypeError('fetch failed after dispatch')
    return { success: true, operation_id: 'op-original', status: 'completed', completed_steps: [0] }
  } }, tool => tools.set(tool.name, tool))
  const result = await tools.get('doraemon_act').execute({ target_id: 't', observation_id: 'o', steps: [{ action: 'close' }] },
    { callId: 'lost-call', signal, agent: { session: { id: 'session' } } })
  assert.equal(result.operation_id, 'op-original')
  assert.deepEqual(calls.map(call => call.path), ['/runtime/act', '/runtime/status'])
  assert.deepEqual(calls[1].body, { request_key: 'lost-call', follow_ms: 0 })
  assert.equal(calls[1].owner, calls[0].owner)
  assert.equal(calls[1].signal, signal)
})

test('argument failures and caller cancellation never trigger response reconciliation', async () => {
  for (const cancelled of [false, true]) {
    const tools = new Map(), controller = new AbortController()
    let count = 0
    registerRuntimeTools({}, { async request() {
      count++
      if (cancelled) controller.abort()
      throw cancelled ? new TypeError('fetch aborted') : new Error('Desktop bridge HTTP 422; invalid action')
    } }, tool => tools.set(tool.name, tool))
    await assert.rejects(tools.get('doraemon_act').execute({ target_id: 't', observation_id: 'o', steps: [{ action: 'close' }] },
      { callId: 'call', signal: controller.signal, agent: { session: { id: 'session' } } }))
    assert.equal(count, 1)
  }
})
