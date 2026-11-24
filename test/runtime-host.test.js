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
})

test('an image storage failure preserves dispatched operation data without unusable coordinates', async () => {
  const tools = new Map()
  const bridge = { async request(path) {
    if (path.includes('/frames/')) return new Uint8Array([1])
    return { success: true, operation_id: 'op-1', status: 'completed', completed_steps: [0],
      observation: { frames: [{ frame_id: 'f_1', width: 100, height: 50 }],
        elements: [{ ref: 'e1', bounds: { frame_id: 'f_1', x: 1, y: 2 } }] } }
  } }
  registerRuntimeTools({ get: () => ({ saveImage: async () => { throw new Error('storage failed') } }) }, bridge,
    tool => tools.set(tool.name, tool))
  const exec = { callId: 'call-1', signal: new AbortController().signal, agent: { session: { id: 'session-1' } } }
  const result = await tools.get('doraemon_act').execute({ target_id: 't', observation_id: 'o', steps: [{ action: 'close' }] }, exec)
  assert.equal(result.success, false)
  assert.equal(result.error.code, 'IMAGE_ATTACHMENT_FAILED')
  assert.equal(result.operation_id, 'op-1')
  assert.deepEqual(result.completed_steps, [0])
  assert.deepEqual(result.observation.frames, [])
  assert.deepEqual(result.observation.elements, [{ ref: 'e1' }])
})
