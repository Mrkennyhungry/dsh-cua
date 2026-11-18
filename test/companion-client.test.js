import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

/**
 * The client half must never occupy `conversation.input.attachments`.
 *
 * That key is a single slot owned by @deepseek-ai/dsh-client-ui-attachment and
 * it renders the draft's image/file thumbnails. 0.4.10-0.4.12 won that cell and
 * then failed to re-render the official entry (StoredEntry has no `registrant`),
 * so composer attachments disappeared entirely. These tests pin the new design:
 * chips live in the composer's overlay list, the attachment slot stays untouched.
 */
function loadClient(form) {
  let definition
  const registrations = []
  const React = {
    Fragment: 'fragment',
    createElement: (type, props, ...children) => ({ type, props: { ...(props || {}), children } }),
    useState: (initial) => [typeof initial === 'function' ? initial() : initial, () => {}],
    useEffect: (effect) => effect(),
  }
  const context = {
    window: { __ModuleLoader__: { load: (value) => { definition = value } } },
  }
  vm.runInNewContext(readFileSync(new URL('../client.js', import.meta.url), 'utf8'), context)
  const plugin = definition.factory((name) => {
    assert.equal(name, 'react')
    return React
  })
  const ctx = {
    configForms: { get: (id) => { assert.equal(id, 'doraemon'); return form } },
    slots: {
      inject: (slot, register) => register(),
      register: (options, component) => { registrations.push({ options, component }); return () => {} },
    },
  }
  plugin.apply(ctx)
  return { registrations, registration: registrations[0] }
}

/** Evaluate function components the way React would, down to host elements. */
function renderComponent(registration, props = {}) {
  let node = registration.component(props)
  while (node && typeof node === 'object' && typeof node.type === 'function') {
    node = node.type(node.props)
  }
  return node
}

function formWith(selection) {
  return {
    getSnapshot: () => ({ value: { companionSelection: selection } }),
    subscribe: (listener) => { listener(); return () => {} },
    set: async () => true,
  }
}

function findButton(node) {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findButton(child)
      if (found) return found
    }
    return null
  }
  if (!node || typeof node !== 'object') return null
  if (node.type === 'button') return node
  for (const child of node.props?.children || []) {
    const found = findButton(child)
    if (found) return found
  }
  return null
}

test('Client never registers into conversation.input.attachments', () => {
  const { registrations } = loadClient(formWith(''))
  assert.equal(registrations.length, 1)
  for (const entry of registrations) {
    assert.notEqual(
      entry.options.name, 'conversation.input.attachments',
      '占用附件单插槽会把官方的图片/文件预览整个顶掉',
    )
  }
})

test('Client registers the chips into the composer overlay list', () => {
  const { registration } = loadClient(formWith(''))
  assert.equal(registration.options.name, 'conversation.input.overlay')
  assert.equal(registration.options.id, 'doraemon-selection')
  assert.equal(registration.options.locale, 'conversation')
})

test('Chip rail is click-through so it cannot block the composer underneath', () => {
  const selection = JSON.stringify([{ window_id: 'win-a', title: '微信', exe: 'Weixin.exe' }])
  const { registration } = loadClient(formWith(selection))
  const rail = renderComponent(registration)
  assert.equal(rail.props.style.pointerEvents, 'none')
  assert.equal(rail.props.style.position, 'absolute')
  const chips = rail.props.children[0]
  assert.equal(chips[0].props.style.pointerEvents, 'auto')
})

test('Client renders one chip per selection and removes only the clicked one', async () => {
  const writes = []
  const selections = [
    { window_id: 'win-a', title: '微信', exe: 'Weixin.exe' },
    { window_id: 'win-b', title: '记事本', exe: 'notepad.exe' },
  ]
  const form = {
    getSnapshot: () => ({ value: { companionSelection: JSON.stringify(selections) } }),
    subscribe: (listener) => { listener(); return () => {} },
    set: async (field, value) => { writes.push({ field, value }); return true },
  }
  const { registration } = loadClient(form)
  const rail = renderComponent(registration)
  assert.equal(rail.props.style.flexWrap, 'wrap')
  const chips = rail.props.children[0]
  assert.equal(chips.length, 2)
  const button = findButton(chips[1])
  assert.ok(button)
  await button.props.onClick()
  assert.equal(JSON.parse(writes[0].value)[0].window_id, 'win-a')
})

test('No selection renders nothing at all', () => {
  const { registration } = loadClient(formWith(''))
  assert.equal(renderComponent(registration), null)
})
