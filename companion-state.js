const NAMESPACE = 'doraemon'
const FIELD = 'companionSelection'
const UI_OWNER = '0'.repeat(64)

function selectionValue(scope) {
  if (!scope?.locked || !scope.window) return ''
  const window = scope.window
  return JSON.stringify({
    title: String(window.title || window.exe || '已选择窗口'),
    exe: String(window.exe || ''),
    pid: Number(window.pid || 0),
    hwnd: Number(window.hwnd || 0),
    window_id: String(scope.window_id || ''),
  })
}

/**
 * Mirror the kernel-wide window lock into the DSH composer chip.
 *
 * The kernel is the source of truth. A non-empty -> empty transition made by the
 * Client chip is a cancellation: Host unlocks the kernel, then clears the chip.
 */
export function installCompanionSelection(ctx, bridge, options = {}) {
  const settings = ctx.settings ?? (typeof ctx.get === 'function' ? ctx.get('settings') : undefined)
  if (!settings || typeof settings.describe !== 'function' || typeof settings.mutate !== 'function') {
    return { dispose: async () => {}, sync: async () => {} }
  }
  const intervalMs = options.intervalMs ?? 1000
  let disposed = false
  let running = false
  let previousCurrent

  function currentSection() {
    const descriptor = settings.describe().find((entry) => entry?.ns === NAMESPACE)
    return {
      value: typeof descriptor?.value?.[FIELD] === 'string' ? descriptor.value[FIELD] : '',
      revision: descriptor?.revision,
    }
  }

  async function sync() {
    if (disposed || running) return
    running = true
    try {
      const [scope, current] = await Promise.all([
        bridge.request('/scope', UI_OWNER, {}),
        Promise.resolve().then(currentSection),
      ])
      if (previousCurrent === undefined) previousCurrent = current.value
      let selected = selectionValue(scope)
      if (previousCurrent && !current.value && scope?.locked) {
        await bridge.request('/windows/unlock', UI_OWNER, { body: {} })
        selected = ''
      }
      if (selected !== current.value) {
        await settings.mutate(NAMESPACE, [{
          op: 'set', path: [FIELD], value: selected,
        }], current.revision)
      }
      previousCurrent = selected
    } catch (error) {
      // A stopped/restarting kernel must not produce an unhandled rejection or a
      // false "cancel". The next poll reconciles both sides again.
      console.debug('[doraemon] selection sync failed: %s', error?.message ?? error)
    } finally {
      running = false
    }
  }

  const timer = setInterval(() => { void sync() }, intervalMs)
  timer.unref?.()
  if (options.autostart !== false) void sync()
  return {
    sync,
    dispose: async () => {
      disposed = true
      clearInterval(timer)
    },
  }
}
