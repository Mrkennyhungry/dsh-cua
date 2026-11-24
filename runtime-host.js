import { defineTool } from '@deepseek-ai/dsh-tools'
import { ownerOf } from './bridge-client.js'

const point = {
  type: 'object', additionalProperties: false,
  properties: {
    ref: { type: 'string' }, frame_id: { type: 'string' },
    x: { type: 'number' }, y: { type: 'number' },
  },
}
const strings = { type: 'array', items: { type: 'string' } }
const condition = {
  type: 'object', additionalProperties: false,
  properties: {
    kind: { type: 'string', required: true, enum: ['text', 'element', 'window', 'state'] },
    text: { type: 'string' }, match: { type: 'string', enum: ['contains', 'exact'] }, ref: { type: 'string' },
    title: { type: 'string' }, state: { type: 'string' },
  },
}
const output = {
  schema: { type: 'object', properties: {}, additionalProperties: true },
  render(_args, value) {
    const { attachments = [], ...result } = value
    return [
      { type: 'text', text: JSON.stringify(result) },
      ...attachments.map(attachment => ({ type: 'image', attachment })),
    ]
  },
}

export function registerRuntimeTools(ctx, bridge, register) {
  async function execute(path, args, exec, operation = false) {
    const owner = ownerOf(exec)
    const body = operation ? { request_key: exec.callId, arguments: args } : args
    let result
    try {
      result = await bridge.request(path, owner, { body, signal: exec.signal, timeoutMs: 70_000 })
      const frames = result?.observation?.frames || []
      if (!frames.length) return result
      const store = ctx.get?.('attachments')
      if (!store?.saveImage) throw new Error('DSH image attachment service is unavailable')
      const attachments = []
      for (const frame of frames) {
        const data = await bridge.request(`/runtime/frames/${frame.frame_id}`, owner, { image: true, signal: exec.signal })
        exec.signal?.throwIfAborted()
        const attachment = await store.saveImage({ data, mediaType: 'image/jpeg', name: `${frame.frame_id}.jpg` })
        exec.signal?.throwIfAborted()
        if (attachment.width !== frame.width || attachment.height !== frame.height) {
          throw new Error('DSH changed image dimensions, so frame coordinates cannot be used')
        }
        attachments.push(attachment)
      }
      return { ...result, attachments }
    } catch (error) {
      if (exec.signal?.aborted || /Desktop bridge HTTP 422\b/.test(String(error?.message))) throw error
      if (result?.observation) {
        const observation = { ...result.observation, frames: [] }
        if (Array.isArray(observation.elements)) observation.elements = observation.elements.map(element => {
          const { bounds, ...semantic } = element
          return semantic
        })
        return { ...result, observation, success: false,
          error: { code: 'IMAGE_ATTACHMENT_FAILED', message: String(error?.message ?? error) } }
      }
      return { success: false, error: { code: 'BRIDGE_UNAVAILABLE', message: String(error?.message ?? error) } }
    }
  }

  const definitions = [
    {
      name: 'doraemon_open',
      description: 'Open an application or URL, connect to an existing browser, or activate a desktop target and return its initial observation.',
      parameters: {
        url: { type: 'string', description: 'URL to open in the dedicated persistent browser, or navigate target_id to.' },
        app: { type: 'string', description: 'Installed application name or executable path.' }, args: strings,
        target_id: { type: 'string', description: 'Existing target to activate.' },
        connect: { type: 'string', description: 'Explicit CDP endpoint for an existing browser.' }, image: { type: 'boolean' },
      }, path: '/runtime/open', operation: true,
    },
    {
      name: 'doraemon_observe',
      description: 'List desktop targets or read a target, returning elements, observation references and an optional image.',
      parameters: {
        target_id: { type: 'string', description: 'Omit to list available targets.' },
        query: { type: 'string', description: 'Semantic filter or visual query for a target.' },
        cursor: { type: 'string', description: 'Continue a paginated observation without rescanning.' },
        image: { type: 'boolean', description: 'Request an image explicitly, otherwise use the runtime image policy.' },
        region: { type: 'object', additionalProperties: false, properties: {
          frame_id: { type: 'string', required: true }, x: { type: 'number', required: true }, y: { type: 'number', required: true },
          width: { type: 'number', required: true }, height: { type: 'number', required: true },
        } },
      }, path: '/runtime/observe',
    },
    {
      name: 'doraemon_act',
      description: 'Execute a short sequence using references or image coordinates from an observation and return a readback. Choose the actions and expected condition for the runtime to execute.',
      parameters: {
        target_id: { type: 'string', required: true }, observation_id: { type: 'string', required: true },
        steps: { type: 'array', required: true, description: 'One to eight ordered actions. Input requires mode and text.', items: { type: 'object', additionalProperties: false, properties: {
          action: { type: 'string', required: true, enum: ['click', 'input', 'key', 'scroll', 'drag', 'select', 'check', 'hover', 'attach', 'close'] },
          target: point, from: point, to: point,
          text: { type: 'string', description: 'Required for input, at most 12000 characters.' },
          mode: { type: 'string', enum: ['replace', 'insert'], description: 'Required for input. Choose replace or insert explicitly.' },
          button: { type: 'string', enum: ['left', 'right'] }, count: { type: 'integer' }, keys: strings,
          direction: { type: 'string', enum: ['up', 'down', 'left', 'right'] },
          amount: { type: 'integer' }, values: strings, checked: { type: 'boolean' }, files: strings,
        } } }, expect: { ...condition, properties: {
          ...condition.properties,
          transient: { type: 'boolean', description: 'For text, arm capture before the selected step is dispatched.' },
          after_step: { type: 'integer', description: 'Zero-based step index, default last step.' },
          timeout_ms: { type: 'integer', description: '100 to 30000 milliseconds, default 5000.' },
        } },
      }, path: '/runtime/act', operation: true,
    },
    {
      name: 'doraemon_wait',
      description: 'Wait for a concrete text, element, window or state condition and return the evidence or a queryable operation handle.',
      parameters: {
        target_id: { type: 'string' }, observation_id: { type: 'string' },
        condition: { ...condition, required: true, description: 'Concrete condition. Set the wait timeout in the top-level timeout_ms field.' },
        timeout_ms: { type: 'integer', description: 'Top-level timeout from 100 to 30000 milliseconds, default 5000.' },
      }, path: '/runtime/wait', operation: true,
    },
    {
      name: 'doraemon_status',
      description: 'Report runtime health or follow an operation for a bounded interval, including completed steps and captured condition evidence.',
      parameters: {
        operation_id: { type: 'string', description: 'Omit to report runtime health.' },
        follow_ms: { type: 'integer', description: 'Follow the operation for 0 to 10000 milliseconds.' },
      }, path: '/runtime/status',
    },
    {
      name: 'doraemon_stop',
      description: 'Cancel an operation or all active operations for this session and return their actual stopping progress.',
      parameters: { operation_id: { type: 'string' } }, path: '/runtime/stop',
    },
  ]
  for (const { path, operation, ...definition } of definitions) {
    register(defineTool({ ...definition, output, execute: (args, exec) => execute(path, args, exec, operation) }))
  }
}
