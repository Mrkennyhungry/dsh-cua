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
