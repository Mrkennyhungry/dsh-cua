/* DSH Client half: show the kernel-selected window above the composer. */
window.__ModuleLoader__.load({
  id: 'dsh-doraemon',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    function SelectionChip({ form }) {
      const [raw, setRaw] = React.useState(() => form.getSnapshot().value?.companionSelection || '')
      const [busy, setBusy] = React.useState(false)
      React.useEffect(() => form.subscribe((snapshot) => {
        setRaw(snapshot.value?.companionSelection || '')
      }), [form])
      if (!raw) return null
      let selection
      try { selection = JSON.parse(raw) } catch { return null }
      const title = selection.title || selection.exe || '已选择窗口'
      const details = [
        `Title: ${title}`,
        `Exe: ${selection.exe || '-'}`,
        `PID: ${selection.pid || '-'}`,
        `HWND: ${selection.hwnd || '-'}`,
      ].join('\n')
      const clear = async () => {
        if (busy) return
        setBusy(true)
        const accepted = await form.set('companionSelection', '')
        if (!accepted) setBusy(false)
      }
      return h('div', {
        title: details,
        style: {
          display: 'inline-flex', alignItems: 'center', gap: 7,
          maxWidth: '100%', padding: '4px 6px 4px 10px',
          border: '1px solid var(--dsw-alias-border-default, rgba(127,127,127,.28))',
          borderRadius: 8,
          background: 'var(--dsw-alias-bg-secondary, rgba(127,127,127,.10))',
          color: 'var(--dsw-alias-text-primary, inherit)',
          fontSize: 12, lineHeight: '20px',
        },
      },
      h('span', {
        style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
      }, title),
      selection.exe ? h('span', {
        style: { color: 'var(--dsw-alias-text-secondary, rgba(127,127,127,.8))', whiteSpace: 'nowrap' },
      }, `(${selection.exe})`) : null,
      h('button', {
        type: 'button',
        title: '取消窗口限定',
        'aria-label': '取消窗口限定',
        disabled: busy,
        onClick: clear,
        style: {
          width: 22, height: 22, padding: 0, border: 0, borderRadius: 5,
          background: 'transparent', color: 'inherit', cursor: busy ? 'default' : 'pointer',
          font: 'inherit', fontSize: 16, lineHeight: '20px',
        },
      }, '×'))
    }

    return {
      inject: ['slots', 'configForms'],
      apply(ctx) {
        const form = ctx.configForms.get('doraemon')
        const Chip = () => h(SelectionChip, { form })
        ctx.slots.inject('conversation.input.dock', () => ctx.slots.register({
          name: 'conversation.input.dock',
          id: 'doraemon-selection',
          order: 100,
        }, Chip))
      },
    }
  },
})
