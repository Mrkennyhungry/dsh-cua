import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

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
  const OfficialAttachments = (props) => ({ type: 'official-attachments', props })
  const ctx = {
    configForms: { get: (id) => { assert.equal(id, 'doraemon'); return form } },
    slots: {
      inject: (slot, register) => register(),
      entries: (slot) => slot === 'conversation.input.attachments'
        ? [{ registrant: '@deepseek-ai/dsh-client-ui-attachment', component: OfficialAttachments }] : [],
      register: (options, component) => registrations.push({ options, component }),
    },
  }
  plugin.apply(ctx)
  return registrations[0]
}

function renderComposite(registration, props = {}) {
  const fragment = registration.component(props)
  const [selectionElement, officialElement] = fragment.props.children
  return {
    fragment, officialElement,
    chip: selectionElement.type(selectionElement.props),
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

test('Client registers inside the composer attachments and cancel clears the shared field', async () => {
  const writes = []
  const form = {
    getSnapshot: () => ({ value: { companionSelection: JSON.stringify({
      title: '微信', exe: 'Weixin.exe', pid: 23744, hwnd: 67282,
    }) } }),
    subscribe: (listener) => { listener(); return () => {} },
    set: async (field, value) => { writes.push({ field, value }); return true },
  }
  const registration = loadClient(form)
  assert.equal(registration.options.name, 'conversation.input.attachments')
  assert.equal(registration.options.id, 'doraemon-selection')
  assert.equal(registration.options.priority, -1000)
  const { chip, officialElement } = renderComposite(registration)
  assert.ok(officialElement)
  const button = findButton(chip)
  assert.ok(button)
  await button.props.onClick()
  assert.deepEqual(writes, [{ field: 'companionSelection', value: '[]' }])
})

test('Client renders a wrapping chip per selection and removes only the clicked one', async () => {
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
  const registration = loadClient(form)
  const { chip: group } = renderComposite(registration)
  assert.equal(group.props.style.flexWrap, 'wrap')
  assert.equal(group.props.style.marginLeft, 16)
  assert.equal(group.props.style.marginRight, 12)
  assert.equal(group.props.style.maxWidth, 'calc(100% - 28px)')
  const chips = group.props.children[0]
  assert.equal(chips.length, 2)
  assert.equal(chips[0].props.style.flex, '0 1 auto')
  const button = findButton(chips[1])
  await button.props.onClick()
  assert.equal(JSON.parse(writes[0].value)[0].window_id, 'win-a')
})

test('Client preserves official attachment props even with no window selected', () => {
  const form = {
    getSnapshot: () => ({ value: { companionSelection: '' } }),
    subscribe: (listener) => { listener(); return () => {} },
    set: async () => true,
  }
  const registration = loadClient(form)
  const onRemoveAttachment = () => {}
  const onRetryFile = () => {}
  const props = {
    attachments: [{ id: 'file-1', kind: 'file' }], canAcceptDrop: true,
    onAddFiles: () => {}, onRemoveAttachment, uploads: { 'file-1': 50 },
    onRetryFile, dropLimits: { maxImages: 10 }, t: (key) => key,
  }
  const { chip, officialElement } = renderComposite(registration, props)
  assert.equal(chip, null)
  assert.ok(officialElement)
  assert.deepEqual(officialElement.props.attachments, props.attachments)
  assert.equal(officialElement.props.onRemoveAttachment, onRemoveAttachment)
  assert.equal(officialElement.props.onRetryFile, onRetryFile)
  assert.equal(officialElement.props.canAcceptDrop, true)
})
