/* DSH Client half: show authorized windows/apps as compact chips above the composer. */
window.__ModuleLoader__.load({
  id: 'dsh-doraemon',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    function parseSelections(raw) {
      if (!raw) return []
      try {
        const value = JSON.parse(raw)
        return (Array.isArray(value) ? value : [value]).filter((item) => item && typeof item === 'object')
      } catch { return [] }
    }

    function SelectionChips({ form }) {
      const [raw, setRaw] = React.useState(() => form.getSnapshot().value?.companionSelection || '')
      const [busy, setBusy] = React.useState(false)
      React.useEffect(() => form.subscribe((snapshot) => {
        // The initial subscription callback has no snapshot argument.
        const value = snapshot?.value ?? form.getSnapshot().value
        setRaw(value?.companionSelection || '')
      }), [form])
      const selections = parseSelections(raw)
      if (!selections.length) return null

      const write = async (value) => {
        if (busy) return
        setBusy(true)
        try { await form.set('companionSelection', value) } finally { setBusy(false) }
      }
      const remove = (item) => write(JSON.stringify(
        selections.filter((candidate) => candidate !== item).map((candidate) => ({
          title: candidate.title || '', exe: candidate.exe || '',
          pid: Number(candidate.pid || 0), hwnd: Number(candidate.hwnd || 0),
          window_id: String(candidate.window_id || ''),
        }))))
      const chip = (item, index) => {
        const title = item.title || item.exe || '已选择窗口'
        const details = [`Title: ${title}`, `Exe: ${item.exe || '-'}`,
          `PID: ${item.pid || '-'}`, `HWND: ${item.hwnd || '-'}`].join('\n')
        return h('div', {
          key: String(item.window_id || `${title}-${index}`), title: details,
          style: {
            display: 'inline-flex', alignItems: 'center', gap: 6,
            flex: '0 1 auto', alignSelf: 'flex-start', minWidth: 0, maxWidth: 'min(100%, 340px)',
            height: 28, padding: '2px 5px 2px 9px', boxSizing: 'border-box',
            border: '1px solid var(--dsw-alias-border-default, rgba(127,127,127,.28))',
            borderRadius: 7,
            background: 'var(--dsw-alias-bg-secondary, rgba(127,127,127,.10))',
            color: 'var(--dsw-alias-text-primary, inherit)',
            fontSize: 12, lineHeight: '18px',
            // The rail itself is click-through so it can never block the
            // composer underneath; only the chips take pointer events.
            pointerEvents: 'auto',
          },
        },
        h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, title),
        item.exe ? h('span', {
          style: { color: 'var(--dsw-alias-text-secondary, rgba(127,127,127,.8))',
            maxWidth: 128, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
        }, `(${item.exe})`) : null,
        h('button', {
          type: 'button', title: `取消限定：${title}`, 'aria-label': `取消限定：${title}`,
          disabled: busy, onClick: () => remove(item),
          style: {
            flex: '0 0 20px', width: 20, height: 20, padding: 0, border: 0, borderRadius: 4,
            background: 'transparent', color: 'inherit', cursor: busy ? 'default' : 'pointer',
            font: 'inherit', fontSize: 15, lineHeight: '18px',
          },
        }, '×'))
      }
      return h('div', {
        'aria-label': 'AI 可操作范围',
        style: {
          position: 'absolute', bottom: 'calc(100% + 6px)', left: 12, right: 12, zIndex: 5,
          display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6,
          boxSizing: 'border-box', padding: 0,
          // Click-through rail: the composer and the attachment rail it sits
          // above stay reachable even while chips are shown.
          pointerEvents: 'none',
        },
      },
      selections.map(chip),
      selections.length > 1 ? h('button', {
        type: 'button', disabled: busy, onClick: () => write(''),
        'aria-label': '清空全部操作范围',
        style: { flex: '0 0 auto', border: 0, background: 'transparent', color: 'inherit',
          opacity: .65, cursor: 'pointer', font: 'inherit', fontSize: 11, padding: '3px 5px',
          pointerEvents: 'auto' },
      }, '全部取消') : null)
    }

    return {
      inject: ['slots', 'configForms'],
      apply(ctx) {
        const form = ctx.configForms.get('doraemon')
        if (!form) return

        // NEVER register into `conversation.input.attachments`.
        //
        // That key is a SINGLE slot owned by @deepseek-ai/dsh-client-ui-attachment.
        // A lower `priority` wins the cell and replaces the official component
        // outright, and the official component is what renders the draft's file
        // and image thumbnails, the upload/retry state and the drop target.
        // 0.4.10-0.4.12 tried to nest that component back in by looking up
        // `entry.registrant`, but a StoredEntry only exposes
        // `{ component, options: { key, id, order, label, priority } }` — there is
        // no `registrant`, so the lookup always missed and the whole attachment
        // rail (images included) rendered as nothing.
        //
        // The chips belong in the composer's overlay list instead: it is a `list`
        // slot rendered in the card's floating anchor, so several plugins coexist
        // and the attachment slot is left untouched.
        // The overlay slot hands the component its own props (sessionId, ...);
        // the config form is closed over here instead of being read from props.
        const ChipsForOverlay = () => h(SelectionChips, { form })

        ctx.slots.inject('conversation.input.overlay', () => ctx.slots.register({
          name: 'conversation.input.overlay',
          id: 'doraemon-selection',
          order: -1,
          locale: 'conversation',
        }, ChipsForOverlay))
      },
    }
  },
})
