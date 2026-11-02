import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

function loadClient(form) {
  let definition
  const registrations = []
  const React = {
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
      register: (options, component) => registrations.push({ options, component }),
    },
  }
  plugin.apply(ctx)
  return registrations[0]
}

function findButton(node) {
  if (!node || typeof node !== 'object') return null
  if (node.type === 'button') return node
  for (const child of node.props?.children || []) {
    const found = findButton(child)
    if (found) return found
  }
  return null
}

test('Client registers a composer dock chip and cancel clears the shared field', async () => {
  const writes = []
  const form = {
    getSnapshot: () => ({ value: { companionSelection: JSON.stringify({
      title: '微信', exe: 'Weixin.exe', pid: 23744, hwnd: 67282,
    }) } }),
    subscribe: (listener) => { listener(); return () => {} },
    set: async (field, value) => { writes.push({ field, value }); return true },
  }
  const registration = loadClient(form)
  assert.equal(registration.options.name, 'conversation.input.dock')
  assert.equal(registration.options.id, 'doraemon-selection')
  const wrapper = registration.component()
  const chip = wrapper.type(wrapper.props)
  const button = findButton(chip)
  assert.ok(button)
  await button.props.onClick()
  assert.deepEqual(writes, [{ field: 'companionSelection', value: '' }])
})

test('Client renders nothing when no window is selected', () => {
  const form = {
    getSnapshot: () => ({ value: { companionSelection: '' } }),
    subscribe: (listener) => { listener(); return () => {} },
    set: async () => true,
  }
  const registration = loadClient(form)
  const wrapper = registration.component()
  assert.equal(wrapper.type(wrapper.props), null)
})
