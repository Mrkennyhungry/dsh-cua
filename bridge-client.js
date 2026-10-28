import { createHash } from 'node:crypto'
import { createKernel } from './kernel.js'

const MAX_JSON_BYTES = 262144
const DEFAULT_TIMEOUT_MS = 15000

export function ownerOf(exec) {
  const id = exec.agent?.session?.id
  if (typeof id !== 'string' || !id) throw new Error('Desktop automation requires a DSH session identity')
  return createHash('sha256').update(id).digest('hex')
}

/**
 * Split Server-Sent-Events frames out of a buffer.
 * Returns the parsed payloads plus whatever tail is still incomplete.
 */
export function extractFrames(buffer) {
  const frames = []
  let rest = buffer
  let index
  while ((index = rest.indexOf('\n\n')) >= 0) {
    const block = rest.slice(0, index)
    rest = rest.slice(index + 2)
    const payload = block
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trim())
      .join('\n')
    if (!payload) continue
    try {
      frames.push(JSON.parse(payload))
    } catch {
      // A malformed frame must not kill the stream; the next one may be fine.
    }
  }
  return { frames, rest }
}

/**
 * A 404 almost always means the endpoint postdates the running kernel. Say that
 * instead of leaving a bare status for the user to interpret.
 */
function httpFailure(status, path) {
  const hint = status === 404
    ? `; ${path} does not exist on the running kernel, which usually means the kernel is older than this plugin — upgrade it with \`update the DSH plugin package\``
    : ''
  return new Error(`Desktop bridge HTTP ${status}${hint}; check bridge logs and task state before retrying`)
}

export function createBridge(config, transport = fetch, options = {}) {
  if (!Number.isInteger(config.port) || config.port < 1024 || config.port > 65535) {
    throw new Error('Invalid loopback port')
  }
  const base = `http://127.0.0.1:${config.port}/api/dsh`
  const kernel = createKernel(config, options)
  const managed = config.manageKernel !== false

  async function send(path, owner, token, { body, signal, timeoutMs = DEFAULT_TIMEOUT_MS, method } = {}) {
    if (typeof token !== 'string' || token.length < 32) {
      throw new Error(`Set ${config.tokenEnv} or enable autoStart so the plugin can start the kernel with a generated token`)
    }
    if (!/^\/[a-z0-9/_-]+$/.test(path) || path.includes('..')) throw new Error('Invalid bridge path')
    signal?.throwIfAborted()
    const combined = signal ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs)
    // Routes are not uniform: some POST endpoints take no arguments. Deriving the
    // verb from "has a body" turned those into a 405, so allow an explicit method.
    const verb = method ?? (body === undefined ? 'GET' : 'POST')
    const payload = body === undefined ? (verb === 'POST' ? '{}' : undefined) : JSON.stringify(body)
    const response = await transport(base + path, {
      method: verb,
      headers: { Authorization: `Bearer ${token}`, 'X-DSH-Owner': owner, 'Content-Type': 'application/json' },
      body: payload, signal: combined, redirect: 'error',
    })
    if (!response.ok) {
      await response.body?.cancel()
      throw httpFailure(response.status, path)
    }
    const reader = response.body.getReader()
    let text = '', size = 0
    const decoder = new TextDecoder()
    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        size += value.byteLength
        if (size > MAX_JSON_BYTES) throw new Error('Desktop result exceeds the response limit')
        text += decoder.decode(value, { stream: true })
      }
      return JSON.parse(text + decoder.decode())
    } finally {
      await reader.cancel().catch(() => {})
    }
  }

  async function request(path, owner, options = {}) {
    const { body, signal } = options
    if (managed) await kernel.ensure(signal)
    try {
      return await send(path, owner, kernel.token, options)
    } catch (error) {
      // Only idempotent reads are retried: re-sending POST /tasks could duplicate real desktop work.
      if (!managed || body !== undefined || signal?.aborted || !(error instanceof TypeError)) throw error
      kernel.forget()
      await kernel.ensure(signal)
      return send(path, owner, kernel.token, options)
    }
  }

  function taskPath(taskId, suffix = '') {
    if (!/^[a-f0-9]{32}$/.test(taskId)) throw new Error('Invalid task id')
    return `/tasks/${taskId}${suffix}`
  }

  async function wait(taskId, owner, signal, waitSeconds) {
    const deadline = Date.now() + waitSeconds * 1000
    while (true) {
      const value = await request(taskPath(taskId), owner, { signal })
      if (value.status !== 'running' || Date.now() >= deadline) return value
      await new Promise((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(signal.reason) }
        const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, 1000)
        signal.addEventListener('abort', abort, { once: true })
        if (signal.aborted) abort()
      })
    }
  }

  async function stream(path, owner, { signal, onEvent, timeoutMs = 600000 } = {}) {
    if (managed) await kernel.ensure(signal)
    const token = kernel.token
    if (typeof token !== 'string' || token.length < 32) {
      throw new Error(`Set ${config.tokenEnv} or enable autoStart so the plugin can start the kernel with a generated token`)
    }
    if (!/^\/[a-z0-9/_-]+$/.test(path) || path.includes('..')) throw new Error('Invalid bridge path')
    const combined = signal
      ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)])
      : AbortSignal.timeout(timeoutMs)
    const response = await transport(base + path, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${token}`,
        'X-DSH-Owner': owner,
        Accept: 'text/event-stream',
      },
      signal: combined,
      redirect: 'error',
    })
    if (!response.ok) {
      await response.body?.cancel()
      throw httpFailure(response.status, path)
    }
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const { frames, rest } = extractFrames(buffer)
        buffer = rest
        for (const frame of frames) onEvent?.(frame)
      }
      const tail = decoder.decode()
      if (tail) buffer += tail
      const { frames } = extractFrames(buffer + '\n\n')
      for (const frame of frames) onEvent?.(frame)
    } finally {
      await reader.cancel().catch(() => {})
    }
  }

  return {
    request, stream, taskPath, wait, kernel,
    get ready() { return kernel.ready },
    shutdown: (force = false) => kernel.stop({ force }),
  }
}
