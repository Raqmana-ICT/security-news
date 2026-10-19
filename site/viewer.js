/* Debug / fallback viewer for news.json. Renders with createElement + textContent only, and
   only links to http(s) URLs: feed data is untrusted. */
(() => {
  'use strict';
  const status = document.getElementById('status');
  const list = document.getElementById('list');

  const safeUrl = value => {
    try {
      const u = new URL(String(value));
      return (u.protocol === 'https:' || u.protocol === 'http:') && u.hostname && !u.username && !u.password ? u.href : null;
    } catch (e) { return null; }
  };
  const el = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };

  fetch('news.json', { cache: 'no-cache' })
    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(data => {
      const items = Array.isArray(data.items) ? data.items : [];
      const failed = (data.sources_failed || []).map(f => f.name + ' (' + f.error + ')');
      status.textContent = `Updated ${data.updated} · ${items.length} items · sources OK: ${(data.sources_ok || []).length}` +
        (failed.length ? ` · failed: ${failed.join(', ')}` : '');
      for (const item of items) {
        const url = safeUrl(item.url);
        if (!url || typeof item.title !== 'string') continue;
        const li = el('li');
        const a = el('a', item.title);
        a.href = url;
        a.target = '_blank';
        a.rel = 'noopener external';
        a.referrerPolicy = 'strict-origin-when-cross-origin';
        a.dir = 'auto';
        li.append(a, el('p', [item.region, item.source, item.type, item.published].join(' · '), 'meta'));
        if (item.excerpt) { const p = el('p', item.excerpt); p.dir = 'auto'; li.append(p); }
        if (Array.isArray(item.also_reported_by) && item.also_reported_by.length) {
          li.append(el('p', 'Also reported by ' + item.also_reported_by.map(r => r.source).join(', '), 'meta'));
        }
        list.append(li);
      }
    })
    .catch(err => { status.textContent = 'Could not load news.json: ' + err.message; });
})();
