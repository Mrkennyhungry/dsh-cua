"""Document-local weak identities and layout changes shared by browser readers."""

STATE_JS = r'''(key) => {
  if (!window[key]) {
    const state = {version: 0, next: 0, ids: new WeakMap(), nodes: new Map()};
    const changed = () => { state.version++; };
    state.observer = new MutationObserver(changed);
    state.roots = new WeakSet();
    state.watch = root => {
      if (state.roots.has(root)) return;
      state.roots.add(root);
      state.observer.observe(root, {subtree: true, childList: true, attributes: true, characterData: true});
    };
    state.watch(document);
    addEventListener('input', changed, true);
    addEventListener('change', changed, true);
    addEventListener('scroll', changed, true);
    addEventListener('resize', changed, true);
    Object.defineProperty(window, key, {value: state, configurable: true});
  }
  return window[key].version;
}'''

RESOLVE_JS = r'''({key, token}) => window[key]?.nodes.get(token)?.deref() || null'''


_SELECTOR_JS = r"""
  const selectorFor = (el, tag, text, cssOnly = false) => {
    let sel = '';
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
      sel = '#' + CSS.escape(el.id);
    } else if (el.getAttribute('data-testid')) {
      sel = `[data-testid="${CSS.escape(el.getAttribute('data-testid'))}"]`;
    } else if (tag === 'input' && el.getAttribute('name')) {
      sel = `input[name="${CSS.escape(el.getAttribute('name'))}"]`;
    } else if (tag === 'input' && el.getAttribute('placeholder')) {
      sel = `input[placeholder*="${CSS.escape(el.getAttribute('placeholder').slice(0, 24))}"]`;
    } else if (el.getAttribute('aria-label')) {
      sel = `${tag}[aria-label*="${CSS.escape(el.getAttribute('aria-label').slice(0, 24))}"]`;
    }
    if (!cssOnly && !sel && text && text.length <= 40 && text !== '[submit]' && !text.startsWith('[') && !text.startsWith('@')) {
      // Some custom controls need a scoped text selector; a bare text selector may not be clickable.
      const firstClass = (typeof el.className === 'string' ? el.className : '')
        .trim().split(/\s+/).filter(Boolean)[0] || '';
      let scope = '';
      const host = el.closest('[class*="drawer"], [class*="modal"], [class*="dialog"], [class*="panel"]');
      if (host) {
        const hostClass = (typeof host.className === 'string' ? host.className : '')
          .trim().split(/\s+/).filter(Boolean)[0] || '';
        if (hostClass) scope = `${host.tagName.toLowerCase()}.${CSS.escape(hostClass)} `;
      }
      if (firstClass) sel = `${scope}${tag}.${CSS.escape(firstClass)}:has-text(${JSON.stringify(text.slice(0, 20))})`;
      if (!sel) sel = `text=${JSON.stringify(text.slice(0, 30))}`;
    }
    if (!sel) {
      const classes = typeof el.className === 'string' ? el.className.trim().split(/\s+/).filter(Boolean) : [];
      sel = tag + classes.slice(0, 2).map(c => '.' + CSS.escape(c)).join('');
    }
    if (cssOnly) {
      // R1：selector 太长不可用（实测 bing 中位 187 字符）。优先序：#id → 属性 →
      // tag.class（逐级加类取首个唯一解）→ 锚定短路径 → nth-path（省略 html/body 前缀）。
      const isUnique = (m) => m.length === 1 && m[0] === el;
      let matches = document.querySelectorAll(sel);
      if (!isUnique(matches)) {
        const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).filter(Boolean) : [];
        for (let n = 1; n <= cls.length && !isUnique(matches); n++) {
          const cand = tag + cls.slice(0, n).map(c => '.' + CSS.escape(c)).join('');
          const m = document.querySelectorAll(cand);
          if (isUnique(m)) { sel = cand; matches = m; }
        }
      }
      if (!isUnique(matches)) {
        // 从最近的带 id 祖先向下锚定；只有同标签并列才用 nth-of-type，
        // 且始终省略 html:nth-of-type(1) > body:nth-of-type(1) 这类废话前缀。
        const segment = (node) => {
          const type = node.tagName.toLowerCase();
          if (node.id) return '#' + CSS.escape(node.id);
          const nodeCls = typeof node.className === 'string' ? node.className.trim().split(/\s+/).filter(Boolean) : [];
          const base = nodeCls.length ? type + '.' + CSS.escape(nodeCls[0]) : type;
          const peers = node.parentElement ? Array.from(node.parentElement.children).filter(p => p.tagName === node.tagName) : [node];
          if (peers.length === 1) return base;
          const sameCls = peers.filter(p => {
            const pc = typeof p.className === 'string' ? p.className.trim().split(/\s+/).filter(Boolean) : [];
            return nodeCls.length && pc[0] === nodeCls[0];
          });
          if (sameCls.length <= 1) return base;
          return `${type}:nth-of-type(${peers.indexOf(node) + 1})`;
        };
        const parts = [];
        for (let node = el; node && node !== document.body && node !== document.documentElement; node = node.parentElement) {
          parts.unshift(segment(node));
          const cand = parts.join(' > ');
          const m = document.querySelectorAll(cand);
          if (isUnique(m) || node.id) { sel = cand; matches = m; break; }
        }
        if (!isUnique(matches)) {
          // 最终兜底：完整结构路径，但同样省略 html/body。
          const full = [];
          for (let node = el; node && node !== document.body && node !== document.documentElement; node = node.parentElement) {
            const type = node.tagName.toLowerCase();
            const peers = node.parentElement ? Array.from(node.parentElement.children).filter(p => p.tagName === node.tagName) : [node];
            full.unshift(`${type}:nth-of-type(${peers.indexOf(node) + 1})`);
          }
          sel = full.join(' > ');
        }
      }
    }
    return sel;
  };
"""


_SNAPSHOT_JS = "(args) => {\n args = args || {};\n" + _SELECTOR_JS + """
  // 自绘组件（div/li/a 实现，没有 role）必须按类名补进来，否则它们既不在快照里、
// 也进不了决策层：自绘页签、翻页与开关可能没有原生 ARIA role，
// 开关是 div[class*=switch]。这三类都实测在控制台里存在但原来一个都看不到。
  const INTERACTIVE = 'button, a[href], input, select, textarea, summary, [role="button"], [role="tab"], [role="menuitem"], [role="option"], [role="link"], [role="checkbox"], [role="radio"], [role="switch"], [onclick], [aria-label]:not([aria-hidden="true"]), [aria-haspopup], [class*="icon" i][role], [class*="tabs__tabitem"], [class*="pagination__turnbtn"], [class*="pagination"] [class*="page"], [class*="switch" i]:not([class*="switch-"]), [class*="dropdown__header"], [class*="dropdown-btn"], [class*="dropdown__value"], [class*="select__trigger"], [class*="select__value"]';
  const OVERLAY_HINT = /modal|dialog|drawer|popup|dropdown|popover|select|overlay|mask|lightbox|ant-modal|tc-modal/i;

  // Deep collect: document.querySelectorAll does NOT pierce shadow DOM —
  // Some cloud consoles render dialogs inside shadow roots,
  // which made web_snapshot completely blind to open dialogs (2026-09-07
  // live session: agent clicked 新增账户, dialog opened, snapshot showed
  // zero dialog elements and a wrong truncated:false, so the agent
  // abandoned web mode for slow desktop OCR).
  const deepQueryAll = (root) => {
    const result = [];
    const seen = new Set();
    const push = (el) => {
      if (!seen.has(el)) { seen.add(el); result.push(el); }
    };
    try {
      for (const el of root.querySelectorAll(INTERACTIVE)) push(el);
      // 第二遍：捞委托点击的组件模式——裸 <li> 选项（如自绘下拉菜单
      // 里 <li>添加分组/编辑分组/删除分组</li>，无 role 无 onclick，React
      // 事件委托）和常见 option/menu 类名。没有这遍，弹出的下拉菜单对快照
      // 完全隐形（agent 只好转昂贵的视觉兜底）。
      for (const el of root.querySelectorAll(
        'li[class*="option" i], [class*="menu-item" i], [class*="list--option" i] li, [class*="dropdown" i] li'
      )) push(el);
      for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) {
          if (args.key) window[args.key].watch(el.shadowRoot);
          for (const sub of deepQueryAll(el.shadowRoot)) push(sub);
        }
      }
    } catch (e) {}
    return result;
  };

  // Cross-boundary parent walk (shadow host aware).
  const parentOf = (node) => {
    if (node.parentElement) return node.parentElement;
    const root = node.getRootNode && node.getRootNode();
    return (root && root.host) ? root.host : null;
  };

  const visibleEl = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return !(style.display === 'none' || style.visibility === 'hidden'
             || rect.width < 2 || rect.height < 2);
  };

  // Is this element inside a currently-visible overlay/dialog?
  const inOpenOverlay = (el) => {
    let node = el;
    for (let i = 0; node && i < 40; i++) {
      if (node.nodeType === 1) {
        let role = '', ariaModal = '', cls = '';
        try {
          role = node.getAttribute('role') || '';
          ariaModal = node.getAttribute('aria-modal') || '';
          cls = (typeof node.className === 'string') ? node.className : '';
        } catch (e) {}
        if (role === 'dialog' || role === 'alertdialog' || ariaModal === 'true'
            || OVERLAY_HINT.test(cls)) {
          try {
            const st = window.getComputedStyle(node);
            if (st.display !== 'none' && st.visibility !== 'hidden') return true;
          } catch (e) {}
        }
      }
      node = parentOf(node);
    }
    return false;
  };

  const describe = (el) => {
    const rect = el.getBoundingClientRect();
    const r = document.documentElement;
    if (rect.bottom < 0 || rect.right < 0 || rect.top > r.clientHeight + 400
        || rect.right > r.clientWidth + 400) return null;  // off-screen
    const tag = el.tagName.toLowerCase();
    let text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
    if (tag === 'input') {
      const t = el.getAttribute('type') || 'text';
      // placeholder 要进 text：goal="搜索终端" 只能靠它匹配（输入框本身没有可见文本）。
      // 注意这个控制台的 placeholder 常是兄弟节点 <p class="...placeholder">，
      // 不在 input 上，所以要就近往上找三层。
      let label = el.getAttribute('placeholder') || '';
      if (!label) {
        let host = el.parentElement;
        for (let depth = 0; depth < 3 && host && !label; depth++, host = host.parentElement) {
          const holder = host.querySelector('[class*="placeholder" i]');
          const value = holder && (holder.textContent || '').trim();
          if (value) label = value;
        }
      }
      text = `[${t}]` + (label ? ` ${String(label).slice(0, 30)}` : '')
             + (el.value ? ` value=${String(el.value).slice(0, 30)}` : '');
    }
    // 图标按钮（纯 <i>/<span> 无文本，如三个点 i[aria-label="Pop_icon_gd"]）：
    // 用 aria-label 当显示名，否则快照里是一行空 text 无法辨认
    if (!text) {
      const al = el.getAttribute('aria-label') || '';
      const title = el.getAttribute('title') || '';
      const alt = (el.querySelector('img[alt]') || {}).alt || el.getAttribute('alt') || '';
      const named = al || title || alt;
      if (named) text = '@' + String(named).slice(0, 40);
      else {
        const xlink = el.querySelector && el.querySelector('use');
        const href = xlink && (xlink.getAttribute('xlink:href') || xlink.getAttribute('href')) || '';
        if (href) text = '@' + href.replace(/^#?icon-/, '').slice(0, 40);
      }
    }
    const sel = selectorFor(el, tag, text);
    const semantic = {};
    if (args.key) {
      const state = window[args.key];
      let token = state.ids.get(el);
      if (!token) { token = String(++state.next); state.ids.set(el, token); }
      state.nodes.set(token, new WeakRef(el));
      while (state.nodes.size > 4000) state.nodes.delete(state.nodes.keys().next().value);
      const labels = el.labels ? Array.from(el.labels).map(label => label.innerText).join(' ') : '';
      const explicit = el.getAttribute('aria-label') || labels || el.getAttribute('title') || el.getAttribute('placeholder');
      const type = (el.getAttribute('type') || '').toLowerCase();
      Object.assign(semantic, {token, name: String(explicit || el.innerText || el.textContent || '').trim().slice(0, 4000),
        input_type: type, enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
        editable: el.isContentEditable || tag === 'textarea' || (tag === 'input' && !['checkbox','radio','file','button','submit'].includes(type)),
        focused: document.activeElement === el});
      if ('value' in el && type !== 'password') semantic.value = String(el.value).slice(0, 12000);
      if ('checked' in el || el.hasAttribute('aria-checked')) semantic.checked = 'checked' in el ? el.checked : el.getAttribute('aria-checked') === 'true';
      if (el.hasAttribute('aria-selected')) semantic.selected = el.getAttribute('aria-selected') === 'true';
      if (tag === 'select') {
        semantic.options = Array.from(el.options).slice(0, 300).map(option => option.label);
        semantic.options_truncated = el.options.length > 300;
        semantic.selected = Array.from(el.selectedOptions).map(option => option.label);
      }
      semantic.role = el.getAttribute('role') || ({button:'button',a:'link',textarea:'textbox',select:'combobox',summary:'button'})[tag]
        || (tag === 'input' ? ({checkbox:'checkbox',radio:'radio',file:'file',button:'button',submit:'button'})[type] || 'textbox' : tag);
    }
    return {
      tag, text: text || '', selector: sel,
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      role: el.getAttribute('role') || '',
      // bbox 给决策层与截图核对用；CSS px，相对视口左上角
      rect: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)],
      href: tag === 'a' ? String(el.getAttribute('href') || '').slice(0, 60) : '',
      // Component signature for the catalog layer: the class names are the only
      // stable DOM contract these design systems expose (some pages have no
      // data-testid / aria contract), so keep the first few, deduped.
      cls: (typeof el.className === 'string' ? el.className : '')
             .trim().split(/\\s+/).filter(Boolean).slice(0, 4),
      ...semantic,
    };
  };

  const all = deepQueryAll(document);
  const overlayOut = [], regularOut = [];
  let hitCap = false, seen = 0;
  const CAP = args.limit || 200, REGULAR_CAP = args.limit ? Math.max(1, CAP - 80) : 120;
  for (const el of all) {
    seen++;
    if (!visibleEl(el)) continue;
    const d = describe(el);
    if (!d) continue;
    if (inOpenOverlay(el)) {
      d.in_overlay = true;
      if (overlayOut.length < 80) overlayOut.push(d);
    } else if (regularOut.length < REGULAR_CAP) {
      regularOut.push(d);
    } else {
      hitCap = true;  // regular bucket full — but KEEP SCANNING: dialog
                      // portals sit at the end of body and must still be
                      // collected (breaking here was the original blindness).
    }
  }
  // Overlay elements are PREPENDED: when a dialog is open, the LLM sees its
  // 确定/取消/表单控件 first instead of 120 table rows that pushed them out.
  const elements = overlayOut.concat(regularOut).slice(0, CAP);
  return {
    elements,
    dialog_open: overlayOut.length > 0,
    overlay_count: overlayOut.length,
    hit_cap: hitCap || (elements.length >= CAP),
    total_interactive_seen: seen,
    ...(args.key ? {text: (document.body?.innerText || '').slice(0, 60000),
       text_truncated: (document.body?.innerText || '').length > 60000} : {}),
  };
}
"""
