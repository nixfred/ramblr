// Ramblr shared shell: header + nav, LISTENING/MUTED status, live neon ticker, mobile tab bar, one SSE
// connection for the page, toast, tiny helpers. Pages call R.on('seg', fn) etc. and set <body data-page="...">.
(function(){
  const $ = s => document.querySelector(s);
  function el(tag, cls, txt){ const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; }
  const api = async (u, o) => { const r = await fetch(u, o); if (!r.ok) throw new Error((await r.text()) || r.status); return r.json(); };
  const post = (u, b) => api(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b || {})});
  let toastT;
  function toast(m, act){ const t = $('#toast'); t.textContent = m; if (act) { const b = el('button', null, act.label); b.style.cssText = 'margin-left:10px;font:inherit;color:inherit;background:transparent;border:1px solid currentColor;border-radius:6px;padding:1px 8px;cursor:pointer'; b.onclick = () => { t.classList.remove('on'); act.fn(); }; t.append(b); t.style.pointerEvents = 'auto'; } t.classList.add('on'); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('on'), act ? 8000 : 2200); }
  const fmtDur = s => { s = Math.round(s || 0); if (s < 60) return s + 's'; const m = Math.round(s / 60); if (m < 60) return m + 'm'; return Math.floor(m / 60) + 'h ' + (m % 60) + 'm'; };
  const esc = s => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  function highlight(node, text, q){
    node.textContent = '';
    const words = (q || '').trim().split(/\s+/).filter(w => w.length > 1).map(esc);
    if (!words.length){ node.textContent = text; return node; }
    const re = new RegExp('(' + words.join('|') + ')', 'ig');
    text.split(re).forEach((part, i) => node.append(i % 2 ? el('mark', null, part) : document.createTextNode(part)));
    return node;
  }
  const handlers = {};
  const on = (ev, fn) => (handlers[ev] = handlers[ev] || []).push(fn);
  const emit = (ev, d) => (handlers[ev] || []).forEach(f => { try { f(d); } catch (e) { console.error(e); } });

  const PAGES = [['today', '/', '◉', 'Today'], ['brief', '/brief', '☀', 'Brief'], ['promises', '/promises', '✓', 'Promises'], ['calendar', '/calendar', '▦', 'Calendar'], ['people', '/people', '◍', 'People'],
                 ['search', '/search', '⌕', 'Search'], ['ideas', '/ideas', '✦', 'Ideas'], ['media', '/media', '▶', 'Media'], ['insights', '/insights', '◈', 'Insights'], ['folders', '/folders', '▤', 'Folders'], ['drafts', '/drafts', '✉', 'Drafts'], ['commands', '/commands', '⇢', 'Commands'], ['digest', '/digest', '☰', 'Digest'], ['health', '/health', '♥', 'Health'], ['settings', '/settings', '⚙', 'Settings']];
  function shell(){
    const page = document.body.dataset.page || '';
    const h = el('header'); h.id = 'shell';
    const bar = el('div', 'bar');
    const logo = el('a', 'logo'); logo.href = '/'; logo.setAttribute('aria-label', 'Ramblr home');
    const img = el('img'); img.src = '/brand/nixfred-icon.svg'; img.alt = 'nixfred';
    logo.append(img, el('span', 'word glitch', 'RAMBLR'));
    const nav = el('nav'); nav.setAttribute('aria-label', 'Pages');
    PAGES.forEach(([id, href, , label]) => { const a = el('a', id === page ? 'on' : '', label); a.href = href; nav.append(a); });
    const st = el('span', 'status starting'); st.id = 'status'; st.append(el('i'), el('span', null, 'CONNECTING')); st.setAttribute('role', 'status');
    const mb = el('button', null, 'Mute'); mb.id = 'mutebtn'; mb.title = 'Mute stops the recorder entirely (Alt+Shift+\\)';
    mb.onclick = async () => { try { const r = await post('/api/mute', {toggle: true}); toast(r.muted ? 'Muted: the recorder is stopped' : 'Listening'); } catch (e) { toast('Failed: ' + e.message); } };
    // call mode (2026-10-24): a caller out of the speakers is a person, not TV. On holds until switched off (4 h safety net)
    const cb = el('button', null, 'Call'); cb.id = 'callbtn'; cb.title = 'Call mode: room lines are talk, never TV, while you are on a call';
    cb.onclick = async () => { const on = cb.dataset.switch !== '1'; try { const r = await post('/api/call', {on}); toast(on ? 'Call mode on: call voices are people, not TV' : (r.live ? 'Call switch off (a call is still detected: ' + r.signals.join(', ') + ')' : 'Call mode off')); } catch (e) { toast('Failed: ' + e.message); } };
    const kb = el('button', 'kbtn'); kb.append(el('span', 'k', 'Ctrl'), el('span', 'k', 'K')); kb.title = 'Command palette'; kb.onclick = () => window.ramblrPalette && window.ramblrPalette.open();
    const menu = el('button', 'menubtn', '☰'); menu.title = 'All commands'; menu.setAttribute('aria-label', 'All commands');
    menu.onclick = () => window.ramblrPalette && window.ramblrPalette.open();
    bar.append(logo, nav, el('div', 'spacer'), st, cb, mb, kb, menu);
    const tk = el('div'); tk.id = 'ticker'; tk.setAttribute('aria-live', 'off');
    tk.append(el('span', 'lbl', 'LIVE'), (() => { const l = el('div', 'lane'); l.append(el('div', 'run')); return l; })());
    h.append(bar, tk);
    document.body.prepend(h);
    const sh = () => document.documentElement.style.setProperty('--shellh', h.offsetHeight + 'px');
    sh(); addEventListener('resize', sh);
    const tabs = el('nav'); tabs.id = 'tabs'; tabs.setAttribute('aria-label', 'Pages');
    const TABS = ['today', 'brief', 'search', 'ideas', 'calendar'];
    PAGES.filter(p => TABS.includes(p[0])).forEach(([id, href, icon, label]) => { const a = el('a', id === page ? 'on' : ''); a.href = href; a.append(el('span', null, icon), document.createTextNode(label)); tabs.append(a); });
    document.body.append(tabs);
    if (!document.body.dataset.nolive) livePanel(h);
    if (!$('#toast')) { const t = el('div'); t.id = 'toast'; t.setAttribute('role', 'status'); document.body.append(t); }
  }

  // ---- B3-7 live summary panel (every page): rolling summary, last conversation card, ask with cited clips ----
  function livePanel(header){
    const p = el('aside'); p.id = 'lsp'; p.setAttribute('aria-label', 'Live summary');
    let min = false; try { min = localStorage.getItem('ramblr.lsp.min') === '1'; } catch (e) {}
    if (!min && innerWidth < 1200) min = true;           // phones: a one-line strip, tap to open
    const hd = el('div', 'lh'); const dot = el('i'); const peek = el('span', 'peek', '');
    const tg = el('button', 'tg', '—'); tg.setAttribute('aria-label', 'Collapse or expand the live summary');
    hd.append(dot, el('b', null, 'LIVE SUMMARY'), peek, tg);
    const body = el('div', 'lb'); const f = el('form'); const q = el('input'); q.placeholder = 'Ask about anything said…'; q.setAttribute('aria-label', 'Ask a question');
    const go = el('button', 'primary', 'Ask'); f.append(q, go);
    const ansBox = el('div');
    p.append(hd, body, f);
    header.after(p);
    const setMin = v => { min = v; p.classList.toggle('min', v); document.body.classList.toggle('lsp-open', !v && innerWidth >= 1200); tg.textContent = v ? '+' : '—';
      try { localStorage.setItem('ramblr.lsp.min', v ? '1' : '0'); } catch (e) {} };
    hd.onclick = () => setMin(!min);
    setMin(min);
    addEventListener('resize', () => document.body.classList.toggle('lsp-open', !min && innerWidth >= 1200));
    const hm = ts => new Date(ts * 1000).toLocaleTimeString(undefined, {hour: 'numeric', minute: '2-digit'});
    async function load(){
      let d; try { d = await api('/api/live'); } catch (e) { return; }
      body.textContent = '';
      if (d.live) {
        dot.classList.add('on'); peek.textContent = d.live.headline;
        body.append(el('div', 'hl', d.live.headline));
        const pp = el('div', 'ppl'); d.live.people.forEach(n => pp.append(el('span', null, n))); body.append(pp);
        const ul = el('ul'); d.live.bullets.forEach(b => ul.append(el('li', null, b))); body.append(ul);
        if (d.live.open && d.live.open.length) { body.append(el('div', 'sec', 'Still open')); const u2 = el('ul'); d.live.open.forEach(b => u2.append(el('li', null, b))); body.append(u2); }
        body.append(el('div', 'meta', `since ${hm(d.live.since)} · ${d.live.lines} lines · updated ${hm(d.live.updated)}`));
      } else if (d.open) {
        dot.classList.add('on'); peek.textContent = 'Conversation under way';
        body.append(el('div', 'hl', 'Conversation under way'), el('div', 'meta', `since ${hm(d.open.ts0)} · ${d.open.lines} lines · the summary appears after a few lines`));
      } else {
        dot.classList.remove('on'); peek.textContent = d.last_card ? 'Last: ' + d.last_card.card.title : 'Quiet';
        body.append(el('div', 'meta', 'Quiet right now. A live summary appears when people talk.'));
      }
      if (d.last_card) {
        const c = d.last_card.card; body.append(el('div', 'sec', 'Last conversation'));
        const a = el('a'); a.href = `/?day=${ymd(d.last_card.ts0)}#seg-${d.last_card.first_seg}`; a.style.color = '#fff'; a.style.textDecoration = 'none';
        a.append(el('div', 'hl', c.title || 'Conversation')); body.append(a);
        if (c.context && c.context !== 'other') body.append(el('span', 'chip m', c.context));
        body.append(el('div', null, c.summary || ''));
        Object.entries(c.sections || {}).filter(([k, v]) => v && v.length).slice(0, 2).forEach(([k, v]) => {
          body.append(el('div', 'sec', k)); const u = el('ul'); v.slice(0, 4).forEach(x => u.append(el('li', null, x))); body.append(u); });
        if (c.decisions && c.decisions.length) { const u = el('ul'); c.decisions.forEach(x => u.append(el('li', null, '✓ ' + x))); body.append(u); }
        body.append(el('div', 'meta', `${hm(d.last_card.ts0)} to ${hm(d.last_card.ts1)} · ${(d.last_card.people || []).join(', ')}`));
      }
      body.append(ansBox);
    }
    f.onsubmit = async e => { e.preventDefault(); const text = q.value.trim(); if (!text) return; setMin(false);
      ansBox.textContent = 'Searching (meaning on the NPU + words), then answering…'; go.disabled = true;
      try { const r = await post('/api/ask', {q: text}); ansBox.textContent = ''; ansBox.append(el('div', 'sec', 'Answer'), el('div', 'ans', r.answer));
        r.citations.forEach(c => { const a = el('a', 'cite'); a.href = `/?day=${c.day}#seg-${c.id}`; if (c.playable) a.append(playButton(c.id)); a.append(document.createTextNode(`#${c.id} ${c.t} ${c.speaker}: ${c.text}`)); ansBox.append(a); });
        body.scrollTop = body.scrollHeight;
      } catch (err) { ansBox.textContent = 'Failed: ' + err.message; } go.disabled = false; };
    on('live', load); on('conv', load); on('reconnect', load);
    load(); setInterval(load, 60000);
  }

  let tickLines = [];
  function renderTicker(newId){
    const run = $('#ticker .run'); if (!run) return;
    run.classList.remove('go'); run.textContent = '';
    if (!tickLines.length) { run.append(el('span', 'it dim', 'waiting for speech…')); return; }
    tickLines.slice(-6).forEach(l => {
      const it = el('span', 'it' + (l.id === newId ? ' new' : ''));
      const b = el('b', null, (l.pending && l.provisional ? l.provisional + '?' : l.speaker) + ':'); b.style.color = l.color || '#8b97ad';
      it.append(b, document.createTextNode(l.text)); run.append(it);
    });
    void run.offsetWidth;                      // restart the scroll from the right
    run.style.setProperty('--dur', Math.max(14, run.scrollWidth / 70) + 's');
    run.classList.add('go');
  }

  function setStatus(s){
    R.state = s;
    if (s.demo && !document.getElementById('demobar')) { const b = el('div', null, 'DEMO · fake data · read-only'); b.id = 'demobar';
      b.style.cssText = 'position:fixed;left:50%;top:6px;transform:translateX(-50%);z-index:300;background:#ff3ea5;color:#05070d;font:800 11px var(--mono);letter-spacing:.2em;padding:3px 12px;border-radius:99px';
      document.body.append(b); }
    const st = $('#status'); if (!st) return;
    let cls = s.capture_state || 'down', txt = (s.capture_state || 'down').toUpperCase();
    if (s.muted) { cls = 'muted'; txt = 'MUTED'; }
    else if (s.family) { cls = 'fallback'; txt = 'FAMILY ARCHIVE'; }
    else if (cls === 'listening' && s.capture_warning) { cls = 'fallback'; txt = 'LISTENING · FALLBACK MIC'; }
    st.className = 'status ' + cls; st.lastChild.textContent = txt;
    st.title = s.capture_warning || (s.muted ? 'Nothing is being recorded' : 'Recording ' + (s.capture_source || ''));
    const mb = $('#mutebtn'); mb.classList.toggle('on', !!s.muted); mb.textContent = s.muted ? 'Unmute' : 'Mute';
    const c = s.call || {}, cb = $('#callbtn');
    if (cb) {
      cb.dataset.switch = c.switch_until ? '1' : '0'; cb.classList.toggle('on', !!c.live); cb.textContent = c.live ? 'On call' : 'Call';
      const why = (c.signals || []).map(x => x === 'switch' ? 'switch (until ' + new Date(c.switch_until * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}) + ')'
        : x === 'heartbeat' ? 'heartbeat from ' + (c.heartbeat_app || 'another machine') : 'gus: ' + c.gus_app);
      cb.title = c.live ? 'Call mode ON: ' + why.join(' · ') + (c.switch_until ? '. Click to switch it off.' : '') : 'Call mode: room lines are talk, never TV, while you are on a call';
      if (c.live && !s.muted) { st.lastChild.textContent = txt + ' · CALL'; }
    }
    emit('status', s);
  }

  let es, down = false;
  function connect(){
    es = new EventSource('/api/stream');
    es.onopen = () => { if (down) { down = false; emit('reconnect'); } };
    es.onerror = () => { down = true; const st = $('#status'); if (st) { st.className = 'status down'; st.lastChild.textContent = 'RECONNECTING'; } };
    es.addEventListener('status', e => setStatus(JSON.parse(e.data)));
    es.addEventListener('seg', e => { const l = JSON.parse(e.data); if (!l.noise && !l.media && !tickLines.some(x => x.id === l.id)) { tickLines.push(l); tickLines = tickLines.slice(-12); renderTicker(l.id); } emit('seg', l); });
    es.addEventListener('command', e => { const c = JSON.parse(e.data);
      toast(`→ ${c.target}: ${c.task}  (${c.status === 'sent' ? 'sent to the router' : c.status === 'stub' ? 'logged, router not set up' : c.status})`);
      let chime = false; try { chime = localStorage.getItem('ramblr.chime') === '1'; } catch (err) {}
      if (chime && c.status === 'queued') { try { const a = new AudioContext(), o = a.createOscillator(), g = a.createGain(); o.frequency.value = 880;
        g.gain.setValueAtTime(0.08, a.currentTime); g.gain.exponentialRampToValueAtTime(0.0001, a.currentTime + 0.25); o.connect(g).connect(a.destination); o.start(); o.stop(a.currentTime + 0.25); } catch (err) {} }
      emit('command', c); });
    ['upd', 'reload', 'idea', 'summary', 'live', 'conv'].forEach(ev => es.addEventListener(ev, e => {
      const d = JSON.parse(e.data);
      if (ev === 'upd') { const i = tickLines.findIndex(x => x.id === d.id); if (i >= 0) { if (d.media) tickLines.splice(i, 1); else tickLines[i] = d; renderTicker(); } }
      emit(ev, d);
    }));
  }

  // one shared player for every ▶ (lines and cited answers); audio exists only for lines under 24 h old
  let player = null, playBtn = null;
  function play(id, btn){
    if (player) { player.pause(); if (playBtn) playBtn.textContent = '▶'; if (player.dataset.id == id) { player = null; return; } }
    player = new Audio('/api/segments/' + id + '/audio'); player.dataset.id = id; playBtn = btn; if (btn) btn.textContent = '■';
    player.onended = () => { if (btn) btn.textContent = '▶'; player = null; };
    player.onerror = async () => { if (btn) btn.textContent = '▶'; player = null;
      try { const r = await fetch('/api/segments/' + id + '/audio'); toast(r.status === 410 ? 'Audio is only kept for 24 hours' : 'No audio for this line'); } catch (e) { toast('No audio for this line'); } };
    player.play().catch(() => {});
  }
  function playButton(id){ const b = el('button', 'pl', '▶'); b.title = 'Play (audio is kept 24 h)'; b.setAttribute('aria-label', 'Play this line');
    b.onclick = e => { e.preventDefault(); e.stopPropagation(); play(id, b); }; return b; }
  const ymd = t => { const d = new Date(t * 1000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };   // local day, like segments.day
  const R = window.R = {$, el, api, post, toast, on, emit, fmtDur, highlight, play, playButton, ymd, state: null};
  window.toast = toast;
  document.addEventListener('DOMContentLoaded', async () => {
    shell();
    connect();
    try { const s = await api('/api/status'); setStatus(s); tickLines = (await api(`/api/segments?day=${s.today}&tail=10`)).filter(l => !l.media && !l.noise).slice(-6); renderTicker(); } catch (e) {}
  });
})();
