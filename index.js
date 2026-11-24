import { Config } from './legacy-host.js'
import { createBridge, ownerOf } from './bridge-client.js'
import { installCursorThinking } from './cursor-state.js'
import { installCompanionSelection } from './companion-state.js'
import { registerRuntimeTools } from './runtime-host.js'

export const name = 'doraemon'
export const inject = ['tools', 'settings']
export { Config }

export function apply(ctx, config) {
  if (!Number.isInteger(config.waitSeconds) || config.waitSeconds < 1 || config.waitSeconds > 3600) {
    throw new Error('waitSeconds must be an integer between 1 and 3600')
  }
  if (!Number.isInteger(config.bootSeconds) || config.bootSeconds < 5 || config.bootSeconds > 900) {
    throw new Error('bootSeconds must be an integer between 5 and 900')
  }
  if (!/^[A-Z_][A-Z0-9_]*$/.test(config.tokenEnv)) throw new Error('Invalid token environment variable')
  const override = process.env.DSH_DESKTOP_PORT
  if (override) {
    const port = Number(override)
    if (!Number.isInteger(port) || port < 1024 || port > 65535) {
      throw new Error('DSH_DESKTOP_PORT must be an integer between 1024 and 65535')
    }
    config = { ...config, port }
  }
  const bridge = createBridge(config)
  const owners = new Set()
  const feedback = config.commandFeedback === true
    ? installCursorThinking(ctx, bridge)
    : { begin() {}, end() {}, dispose: async () => {} }
  const selection = installCompanionSelection(ctx, bridge)
  const lifetime = new AbortController()
  if (config.commandFeedback === true) {
    bridge.request('/companion/start', '0'.repeat(64), {
      body: { client_pid: process.pid }, signal: lifetime.signal,
    }).catch(() => {})
  }
  registerRuntimeTools(ctx, bridge, tool => {
    ctx.tools.register({ ...tool, async execute(args, exec) {
      owners.add(ownerOf(exec))
      feedback.begin(exec.agent?.session, tool.name, args)
      const onAbort = () => feedback.end(exec.agent?.session)
      exec.signal?.addEventListener('abort', onAbort, { once: true })
      try { return await tool.execute(args, exec) }
      finally { exec.signal?.removeEventListener('abort', onAbort) }
    } })
  })
  ctx.effect(() => async () => {
    lifetime.abort()
    await selection.dispose()
    await feedback.dispose()
    if (bridge.ready) {
      await Promise.allSettled([...owners].map(owner => bridge.request('/runtime/stop', owner, {
        body: {}, start: false, signal: AbortSignal.timeout(3000),
      })))
      await bridge.request('/companion/shutdown', '0'.repeat(64), {
        body: {}, start: false, signal: AbortSignal.timeout(3000),
      }).catch(() => {})
    }
    if (!config.keepAlive) await bridge.shutdown(true)
  })
}
