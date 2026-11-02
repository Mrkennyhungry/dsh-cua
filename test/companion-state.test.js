import test from 'node:test'
import assert from 'node:assert/strict'
import { installCompanionSelection } from '../companion-state.js'

function fixture() {
  const state = {
    section: { companionSelection: '' }, revision: 1,
    scope: { locked: false }, mutations: [], requests: [],
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
      if (path === '/windows/unlock') {
        state.scope = { locked: false }
        return { success: true }
      }
      throw new Error(`unexpected path ${path}`)
    },
  }
  const sync = installCompanionSelection({ settings }, bridge, { intervalMs: 3600000, autostart: false })
  return { state, sync }
}

test('selection synchronizer mirrors a locked window into the composer field', async () => {
  const { state, sync } = fixture()
  state.scope = {
    locked: true, window_id: 'window-7',
    window: { title: '微信', exe: 'Weixin.exe', pid: 23744, hwnd: 67282 },
  }
  await sync.sync()
  const selected = JSON.parse(state.section.companionSelection)
  assert.equal(selected.title, '微信')
  assert.equal(selected.exe, 'Weixin.exe')
  assert.equal(selected.hwnd, 67282)
  await sync.dispose()
})

test('clearing the chip calls the kernel unlock instead of only hiding text', async () => {
  const { state, sync } = fixture()
  state.scope = {
    locked: true, window_id: 'window-7',
    window: { title: '微信', exe: 'Weixin.exe', pid: 23744, hwnd: 67282 },
  }
  await sync.sync()
  state.section.companionSelection = '' // what the Client cancel button writes
  await sync.sync()
  assert.ok(state.requests.some((request) => request.path === '/windows/unlock'))
  assert.equal(state.section.companionSelection, '')
  await sync.dispose()
})

test('an initially empty field does not cancel a pre-existing kernel lock', async () => {
  const { state, sync } = fixture()
  state.scope = {
    locked: true, window_id: 'window-7',
    window: { title: '微信', exe: 'Weixin.exe', pid: 1, hwnd: 2 },
  }
  await sync.sync()
  assert.equal(state.requests.filter((request) => request.path === '/windows/unlock').length, 0)
  await sync.dispose()
})

test('missing settings service leaves the Host plugin operational', async () => {
  const sync = installCompanionSelection({}, { request: async () => ({ locked: false }) })
  await sync.sync()
  await sync.dispose()
})
