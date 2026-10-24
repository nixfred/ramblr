// Ctrl+K command palette, shared by every Ramblr page. Pages add their own commands with
// window.ramblrCommands.push({id, title, hint, run}) before or after this script loads.
(function(){
  const cmds = window.ramblrCommands = window.ramblrCommands || [];
  const j = async (u, o) => { const r = await fetch(u, o); if (!r.ok) throw new Error(await r.text()); return r.json(); };
  const post = (u, b) => j(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b || {})});
  const say = m => (window.toast ? window.toast(m) : console.log(m));
  const base = [
    {id: 'go-transcript', title: 'Go to Transcript', hint: 'page', run: () => location.href = '/'},
    {id: 'go-brief', title: 'Go to Morning brief', hint: 'page', run: () => location.href = '/brief'},
    {id: 'go-promises', title: 'What did I promise?', hint: 'page', run: () => location.href = '/promises'},
    {id: 'go-calendar', title: 'Go to Calendar', hint: 'page', run: () => location.href = '/calendar'},
    {id: 'go-people', title: 'Go to People', hint: 'page', run: () => location.href = '/people'},
    {id: 'go-search', title: 'Search the transcript…', hint: 'page', run: () => location.href = '/search'},
    {id: 'ask', title: 'Ask your transcript (AI answer with citations)…', hint: 'ask', run: () => location.href = '/search?ask=1'},
    {id: 'go-ideas', title: 'Go to Ideas', hint: 'page', run: () => location.href = '/ideas'},
    {id: 'go-media', title: 'Go to Media log (what played)', hint: 'page', run: () => location.href = '/media'},
    {id: 'go-imports', title: 'Import Zoom transcripts or Wispr dictations', hint: 'page', run: () => location.href = '/imports'},
    {id: 'go-iphone', title: 'iPhone ramble capture (install, uploads)', hint: 'page', run: () => location.href = '/iphone'},
    {id: 'go-folders', title: 'Go to Folders (auto-sorted cards and ideas)', hint: 'page', run: () => location.href = '/folders'},
    {id: 'go-quotes', title: 'Podcast prep: pull quotes', hint: 'page', run: () => location.href = '/quotes'},
    {id: 'go-drafts', title: 'Go to Drafts (follow-ups, never sent)', hint: 'page', run: () => location.href = '/drafts'},
    {id: 'go-commands', title: 'Go to Commands ("Larry, tell <bot> to ...")', hint: 'page', run: () => location.href = '/commands'},
    {id: 'go-plugins', title: 'Go to Plugins and webhooks', hint: 'page', run: () => location.href = '/plugins'},
    {id: 'go-insights', title: 'Go to Insights (facts, goals, patterns, topics, talk stats)', hint: 'page', run: () => location.href = '/insights'},
    {id: 'go-digest', title: 'Go to Weekly digest', hint: 'page', run: () => location.href = '/digest'},
    {id: 'go-health', title: 'Go to Health (mic, NPU, storage, services)', hint: 'page', run: () => location.href = '/health'},
    {id: 'go-setup', title: 'First-run setup wizard', hint: 'page', run: () => location.href = '/setup'},
    {id: 'go-settings', title: 'Go to Settings', hint: 'page', run: () => location.href = '/settings'},
    {id: 'family', title: 'Family archive mode on / off (separate DB, no AI)', hint: 'family', run: async () => { const s = await j('/api/status'); const r = await post('/api/family', {on: !s.family}); say(r.family ? 'Family archive ON: new lines go only to the family archive' : 'Family archive off'); }},
    {id: 'go-family', title: 'Open the family archive', hint: 'page', run: () => location.href = '/family'},
    {id: 'mute', title: 'Mute / unmute recording', hint: 'Alt+Shift+\\', run: async () => { const r = await post('/api/mute', {toggle: true}); say(r.muted ? 'Muted: recorder stopped' : 'Listening'); }},
    {id: 'mic', title: 'Choose microphone…', hint: 'mic', sub: async () => {
      const m = await j('/api/mics');
      return m.mics.map(x => ({id: 'mic:' + x.name, title: (x.recording ? '● ' : '○ ') + x.description + (x.home ? '  (home)' : ''),
        hint: x.available ? (x.recording ? 'recording' : 'available') : 'unplugged',
        run: async () => { const r = await post('/api/mics', {source: x.home ? null : x.name}); say('Recording: ' + ((r.mics.find(y => y.recording) || {}).description || 'none')); }}));
    }},
  ];
  base.forEach(c => { if (!cmds.some(x => x.id === c.id)) cmds.push(c); });

  let box, inp, list, items = [], sel = 0, stack = null;
  function build(){
    box = document.createElement('div'); box.id = 'kpal'; box.setAttribute('role', 'dialog'); box.setAttribute('aria-label', 'Command palette');
    box.innerHTML = '<div class="kp"><input type="text" placeholder="Type a command…  (Esc closes)" aria-label="Command"><div class="kl" role="listbox"></div></div>';
    document.body.append(box);
    inp = box.querySelector('input'); list = box.querySelector('.kl');
    box.addEventListener('mousedown', e => { if (e.target === box) close(); });
    inp.addEventListener('input', () => { sel = 0; draw(); });
    inp.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(sel + 1, items.length - 1); draw(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(sel - 1, 0); draw(); }
      else if (e.key === 'Enter') { e.preventDefault(); pick(items[sel]); }
      else if (e.key === 'Escape') { e.preventDefault(); if (stack) { stack = null; inp.value = ''; draw(); } else close(); }
    });
    const st = document.createElement('style');
    st.textContent = '#kpal{position:fixed;inset:0;background:#000b;z-index:100;display:none;align-items:flex-start;justify-content:center;padding-top:12vh}' +
      '#kpal.on{display:flex}#kpal .kp{width:min(640px,calc(100vw - 20px));background:var(--panel,#111a2e);border:1px solid var(--teal,#2fb7c9);border-radius:14px;box-shadow:0 0 40px #2de2e633;overflow:hidden}' +
      '#kpal input{width:100%;border:0;border-bottom:1px solid var(--line,#1b2740);border-radius:0;background:transparent;color:var(--fg,#d7e0f0);font:16px ui-monospace,monospace;padding:14px 16px;outline:none}' +
      '#kpal .kl{max-height:52vh;overflow:auto;padding:6px}#kpal .ki{display:flex;justify-content:space-between;gap:10px;padding:10px 12px;border-radius:8px;cursor:pointer;min-height:44px;align-items:center}' +
      '#kpal .ki.on{background:#0e3a44;box-shadow:inset 3px 0 0 var(--teal,#2fb7c9)}#kpal .ki small{color:var(--dim,#7b8aa5);font-family:ui-monospace,monospace}';
    document.head.append(st);
  }
  function source(){ return stack || cmds; }
  function draw(){
    const q = inp.value.trim().toLowerCase();
    items = source().filter(c => !q || (c.title + ' ' + (c.hint || '')).toLowerCase().includes(q));
    list.textContent = '';
    items.forEach((c, i) => {
      const d = document.createElement('div'); d.className = 'ki' + (i === sel ? ' on' : ''); d.setAttribute('role', 'option');
      const t = document.createElement('span'); t.textContent = c.title; const h = document.createElement('small'); h.textContent = c.hint || '';
      d.append(t, h); d.onclick = () => pick(c); list.append(d);
      if (i === sel) d.scrollIntoView({block: 'nearest'});
    });
  }
  async function pick(c){
    if (!c) return;
    try {
      if (c.sub) { stack = await c.sub(); inp.value = ''; sel = 0; draw(); return; }
      close(); await c.run();
    } catch (e) { say('Failed: ' + e.message); }
  }
  function open(){ if (!box) build(); stack = null; inp.value = ''; sel = 0; draw(); box.classList.add('on'); inp.focus(); }
  function close(){ if (box) box.classList.remove('on'); }
  window.ramblrPalette = {open, close};
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); box && box.classList.contains('on') ? close() : open(); }
  });
})();
