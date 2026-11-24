import test from 'node:test'
import assert from 'node:assert/strict'
import { installCompanionSelection } from '../companion-state.js'

const a = { window_id: 'win-a', title: '微信', exe: 'Weixin.exe', pid: 23744, hwnd: 67282 }
const b = { window_id: 'win-b', title: '记事本', exe: 'notepad.exe', pid: 100, hwnd: 9001 }

function fixture() {
  const state = {
    section: { companionSelection: '' }, revision: 1,
    scope: { locked: false, selections: [] }, mutations: [], requests: [],
  }
  const settings = {
    describe: () => [{ ns: 'doraemon', value: { ...state.section }, revision: state.revision }],
    mutate: async (ns, ops, revision) => {
      state.mutations.push({ ns, ops, revision })
      for (const op of ops) {
        if (op.op === 'set') state.section[op.path[0]] = op.value
      }
      state.revision += 1
      return { ok: true }
    },
  }
  const bridge = {
    request: async (path, owner, options) => {
      state.requests.push({ path, owner, options })
      if (path === '/scope') return state.scope
      const match = path.match(/^\/scope\/windows\/([^/]+)\/release$/)
      if (match) {
        const id = decodeURIComponent(match[1])
        state.scope.selections = state.scope.selections.filter((item) => item.window_id !== id)
        state.scope.locked = state.scope.selections.length > 0
        return { success: true }
      }
      if (path === '/windows/unlock') {
        state.scope = { locked: false, selections: [] }
        return { success: true }
      }
      throw new Error(`unexpected path ${path}`)
    },
  }
  const sync = installCompanionSelection({ settings }, bridge, { intervalMs: 3600000, autostart: false })
  return { state, sync }
}

test('selection synchronizer mirrors every authorized window', async () => {
  const { state, sync } = fixture()
  state.scope = { locked: true, selections: [a, b] }
  await sync.sync()
  const selected = JSON.parse(state.section.companionSelection)
  assert.deepEqual(selected.map((item) => item.window_id), ['win-a', 'win-b'])
  assert.equal(selected[0].title, '微信')
  await sync.dispose()
})

test('removing one chip unlocks only that selection', async () => {
  const { state, sync } = fixture()
  state.scope = { locked: true, selections: [a, b] }
  await sync.sync()
  state.section.companionSelection = JSON.stringify([b])
  await sync.sync()
  assert.deepEqual(state.requests.filter((r) => r.path.endsWith('/release')).map((r) => r.path), ['/scope/windows/win-a/release'])
  assert.deepEqual(JSON.parse(state.section.companionSelection).map((item) => item.window_id), ['win-b'])
  assert.equal(state.scope.locked, true)
  await sync.dispose()
})

test('clearing all chips unlocks every selection', async () => {
  const { state, sync } = fixture()
  state.scope = { locked: true, selections: [a, b] }
  await sync.sync()
  state.section.companionSelection = ''
  await sync.sync()
  assert.equal(state.requests.filter((r) => r.path.endsWith('/release')).length, 2)
  assert.equal(state.section.companionSelection, '[]')
  assert.equal(state.scope.locked, false)
  await sync.dispose()
})

test('legacy single-window scope is mirrored as a one-item array', async () => {
  const { state, sync } = fixture()
  state.scope = { locked: true, window_id: a.window_id, window: a }
  await sync.sync()
  assert.deepEqual(JSON.parse(state.section.companionSelection).map((item) => item.window_id), ['win-a'])
  await sync.dispose()
})

test('an initially empty field does not cancel a pre-existing kernel lock', async () => {
  const { state, sync } = fixture()
  state.scope = { locked: true, selections: [a] }
  await sync.sync()
  assert.equal(state.requests.filter((request) => request.path.endsWith('/release')).length, 0)
  await sync.dispose()
})

test('missing settings service leaves the Host plugin operational', async () => {
  const sync = installCompanionSelection({}, { request: async () => ({ locked: false }) })
  await sync.sync()
  await sync.dispose()
})

test('selection polling and disposal leave a stopped kernel idle', async () => {
  const { state, sync } = fixture()
  // The production bridge supplies a readiness property. The fixture's other
  // tests omit it to exercise a running unmanaged bridge.
  await sync.dispose()
  const settings = { describe: () => [], mutate: async () => assert.fail('unexpected mutation') }
  const idle = installCompanionSelection({ settings }, {
    ready: false,
    request: async () => assert.fail('polling must not start the kernel'),
  }, { autostart: false })
  await idle.sync()
  await idle.dispose()
  assert.equal(state.requests.length, 0)
})
