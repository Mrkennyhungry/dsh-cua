import { ownerOf } from './bridge-client.js'

export function actionForTool(name, args = {}) {
  if (args.button === 'right' && name.includes('click')) return 'right_click'
  for (const [part, action] of [['right_click','right_click'],['click','click'],['invoke','click'],['type','type'],['key','key'],['scroll','scroll'],['swipe','drag'],['drag','drag'],['analyze','observe'],['uia','observe'],['web','observe']]) {
    if (name.includes(part)) return action
  }
  return 'thinking'
}

/** Summon only for Doraemon work; never for an unrelated chat turn. */
export function installCursorThinking(ctx, bridge) {
  const queues = new Map(), owners = new Map()
  let disposed = false
  const eligible = name => name?.startsWith('doraemon_') && !['doraemon_health','doraemon_status','doraemon_stop','doraemon_steer','doraemon_cleanup','doraemon_cursor','doraemon_highlight'].includes(name)
  function setState(session, active, action = 'thinking') {
    if (disposed || !session?.id) return
    let owner
    try { owner = ownerOf({ agent: { session } }) } catch { return }
    if (active) owners.set(owner, session)
    else owners.delete(owner)
    const update = (queues.get(owner) ?? Promise.resolve()).catch(() => {}).then(async () => {
      if (disposed && active) return
      try {
        await bridge.request('/mascot/state', owner, {
          body: { active, action, client_pid: process.pid }, timeoutMs: 3000,
        })
      } catch { /* Decorations must never block automation. */ }
    })
    queues.set(owner, update)
    void update.finally(() => { if (queues.get(owner) === update) queues.delete(owner) })
  }
  function begin(session, name, args) {
    if (eligible(name)) setState(session, true, actionForTool(name,args))
  }
  if (typeof ctx.on === 'function') ctx.on('session/event', (session, event) => {
    if (event?.type === 'tool/call') begin(session, event.data?.name, event.data?.args)
    else if (event?.type === 'step/start') {
      const owner = session?.id ? ownerOf({ agent: { session } }) : null
      if (owners.has(owner)) setState(session,true)
    } else if (event?.type === 'turn/end') {
      const owner = session?.id ? ownerOf({ agent: { session } }) : null
      if (owners.has(owner)) setState(session,false)
    }
  })
  const timer = setInterval(() => {
    for (const session of owners.values()) setState(session,true,null)
  },8000)
  timer.unref?.()
  return {
    begin,
    end(session) { setState(session,false) },
    async flush() { await Promise.allSettled([...queues.values()]) },
    async dispose() {
      clearInterval(timer)
      disposed = true
      await Promise.allSettled([...queues.values()])
      if (bridge.ready) await Promise.allSettled([...owners].map(([owner]) =>
        bridge.request('/mascot/state',owner,{body:{active:false,client_pid:process.pid},timeoutMs:3000})))
      owners.clear()
    },
  }
}
