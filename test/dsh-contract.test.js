import test from 'node:test'
import assert from 'node:assert/strict'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { ToolExecutionPipeline } from '@deepseek-ai/dsh-tools/pipeline'

// Exercise the installed SDK's real validation and materialization before the
// runtime adopts nested actions and durable image attachments.
function fixture() {
  let calls = 0
  const tool = defineTool({
    name: 'p0_contract_probe',
    description: 'Test nested actions and native image output.',
    parameters: {
      steps: {
        type: 'array', required: true,
        items: {
          type: 'object', additionalProperties: false,
          properties: {
            action: { type: 'string', required: true, enum: ['click', 'input'] },
            ref: { type: 'string', required: true },
          },
        },
      },
    },
    output: {
      schema: { type: 'object', properties: {}, additionalProperties: true },
      render: (_args, value) => [
        { type: 'text', text: JSON.stringify(value) },
        { type: 'image', attachment: value.image },
      ],
    },
    async execute(_args, exec) {
      calls += 1
      assert.equal(exec.callId, 'p0-call')
      assert.ok(exec.signal instanceof AbortSignal)
      return { image: { attachmentId: 'p0-image', mediaType: 'image/png', bytes: 128, width: 80, height: 50 } }
    },
  })
  const results = []
  const pipeline = new ToolExecutionPipeline({
    tools: { checkGuards: () => undefined },
    waterfall: (_subject, _event, ...args) => args.at(-1)(),
    events: { dispatch: () => [(_exec, result) => results.push(result)] },
    logger: { warn: message => assert.fail(message) },
  }, { get: name => name === tool.name ? tool : undefined })
  async function run(args, signal = new AbortController().signal) {
    const prepared = await pipeline.prepare({ name: tool.name, arguments: args, callId: 'p0-call', signal })
    const outcome = prepared.kind === 'dispatch' ? await pipeline.dispatch(prepared.exec) : prepared
    const result = await pipeline.complete(prepared.exec, outcome)
    assert.equal(results.at(-1), result)
    return result
  }
  return { tool, run, calls: () => calls }
}

test('installed DSH pipeline accepts nested steps and preserves native image attachments', async () => {
  const { tool, run, calls } = fixture()
  assert.deepEqual(tool.parameters.required, ['steps'])
  assert.deepEqual(tool.parameters.properties.steps.items.required, ['action', 'ref'])
  const result = await run({ steps: [{ action: 'click', ref: 'e1' }] })
  assert.equal(result.isError, false)
  assert.equal(calls(), 1)
  assert.equal(result.content.length, 2)
  assert.deepEqual(result.content[1], { type: 'image', attachment: result.value.image })
  assert.ok(Object.isFrozen(result.content[1].attachment))
})

test('installed DSH pipeline rejects invalid nested actions before dispatch', async () => {
  const { run, calls } = fixture()
  for (const args of [
    {},
    { steps: [{ action: 'click' }] },
    { steps: [{ action: 'unknown', ref: 'e1' }] },
    { steps: [{ action: 'click', ref: 'e1', extra: true }] },
  ]) {
    assert.equal((await run(args)).isError, true)
  }
  assert.equal(calls(), 0)
})

test('installed DSH pipeline cancels before executing an image-producing action', async () => {
  const { run, calls } = fixture()
  const controller = new AbortController()
  controller.abort()
  assert.equal((await run({ steps: [{ action: 'click', ref: 'e1' }] }, controller.signal)).isError, true)
  assert.equal(calls(), 0)
})
