const NAMESPACE = 'doraemon'
const FIELD = 'companionSelection'
const UI_OWNER = '0'.repeat(64)

function selectionValue(scope) {
  const selections = Array.isArray(scope?.selections)
    ? scope.selections
    : (scope?.locked && scope.window ? [{ ...scope.window, window_id: scope.window_id }] : [])
  return JSON.stringify(selections.map((window) => ({
    title: String(window.title || window.exe || '已选择窗口'),
    exe: String(window.exe || ''),
    pid: Number(window.pid || 0),
    hwnd: Number(window.hwnd || 0),
    window_id: String(window.window_id || ''),
  })))
}

function parseSelections(raw) {
  if (!raw) return []
  try {
    const value = JSON.parse(raw)
    return (Array.isArray(value) ? value : [value]).filter((item) => item && typeof item === 'object')
  } catch { return [] }
}

function selectionIds(raw) {
  return parseSelections(raw).map((item) => String(item.window_id || '')).filter(Boolean)
}

/**
 * Mirror every kernel-authorized window into compact DSH composer chips.
 *
 * The kernel is the source of truth. IDs removed by the Client are unlocked
 * individually; removing one chip never clears the other authorized entries.
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
      const currentIds = new Set(selectionIds(current.value))
      const removed = selectionIds(previousCurrent).filter((id) => !currentIds.has(id))
      if (removed.length && scope?.locked) {
        for (const id of removed) {
          const result = await bridge.request(`/windows/${encodeURIComponent(id)}/unlock`, UI_OWNER, { body: {} })
          if (result?.success === false) throw new Error(result.error || `failed to unlock ${id}`)
        }
        selected = JSON.stringify(parseSelections(selected).filter((item) => !removed.includes(String(item.window_id || ''))))
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
