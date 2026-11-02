/* پژوهش و جستجو — فرانت‌اند بدون وابستگی خارجی */
'use strict';

(function () {
  // ------------------------------------------------------------ helpers
  const $ = (sel, root) => (root || document).querySelector(sel);
  const FA = '۰۱۲۳۴۵۶۷۸۹';
  const toFa = (v) => String(v).replace(/\d/g, (d) => FA[d]);
  // Convert digits to Persian only in tokens without Latin letters (keeps domains like a2b.com intact).
  const faSmart = (text) => String(text).split(/(\s+)/).map((tok) => (/[A-Za-z]/.test(tok) ? tok : toFa(tok))).join('');
  const escapeHtml = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else el.setAttribute(k, v);
    });
    children.forEach((c) => { if (c) el.append(c); });
    return el;
  }

  const els = {
    form: $('#research-form'), topic: $('#topic'), counter: $('#counter'), topicError: $('#topic-error'),
    startBtn: $('#start-btn'), stopBtn: $('#stop-btn'), notice: $('#notice'),
    progressSection: $('#progress-section'), reportSection: $('#report-section'), inputSection: $('#input-section'),
    barFill: $('#bar-fill'), bar: $('.bar'), progressLabel: $('#progress-label'),
    stages: Array.from(document.querySelectorAll('#stages li')),
    activity: $('#activity'), findings: $('#findings'), findingsCount: $('#findings-count'),
    report: $('#report'), copyBtn: $('#copy-btn'), downloadBtn: $('#download-btn'), printBtn: $('#print-btn'),
    historyList: $('#history-list'), historyEmpty: $('#history-empty'), sidebar: $('#sidebar'),
    historyToggle: $('#history-toggle'), overlay: $('#overlay'), toast: $('#toast'),
    health: $('#health'), healthText: $('#health-text'), themeToggle: $('#theme-toggle'), themeIcon: $('#theme-icon'),
  };

  const STAGES = ['planning', 'gathering', 'verifying', 'writing'];
  const STAGE_LABELS = { pending: 'در انتظار', running: 'در حال اجرا', done: 'انجام‌شده', failed: 'ناموفق' };
  const BADGES = { pending: 'در حال بررسی', verified: 'تأییدشده', rejected: 'ردشده', unchecked: 'بررسی‌نشده' };

  const state = {
    jobId: null, es: null, finished: true, retries: 0, lastSeq: -1, stage: 'planning',
    findings: new Map(), reportMd: '', reportId: null, noticeSource: null,
  };

  // ------------------------------------------------------------ notices / toast
  function showNotice(message, kind, source) {
    els.notice.textContent = message;
    els.notice.dataset.kind = kind || 'info';
    els.notice.hidden = false;
    state.noticeSource = source || 'general';
  }
  function clearNotice(source) {
    if (source && state.noticeSource !== source) return;
    els.notice.hidden = true;
    els.notice.textContent = '';
    state.noticeSource = null;
  }
  let toastTimer = null;
  function toast(message) {
    els.toast.textContent = message;
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { els.toast.hidden = true; }, 2200);
  }

  // ------------------------------------------------------------ theme
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    els.themeToggle.setAttribute('aria-pressed', String(theme === 'dark'));
    els.themeIcon.textContent = theme === 'dark' ? '☀' : '☾';
    try { localStorage.setItem('theme', theme); } catch (e) { /* ignore */ }
  }
  els.themeToggle.addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });
  applyTheme(document.documentElement.dataset.theme || 'light');

  // ------------------------------------------------------------ health
  async function checkHealth() {
    try {
      const res = await fetch('/api/health', { cache: 'no-store' });
      const data = await res.json();
      els.health.dataset.state = data.llm_available ? 'ok' : 'bad';
      els.healthText.textContent = data.llm_available ? `مدل آماده: ${data.model}` : 'مدل در دسترس نیست';
      els.health.title = data.detail || '';
      if (!data.llm_available && state.finished) showNotice(data.detail || 'مدل در دسترس نیست.', 'warn', 'health');
      else clearNotice('health');
    } catch (e) {
      els.health.dataset.state = 'bad';
      els.healthText.textContent = 'سرور در دسترس نیست';
      if (state.finished) showNotice('ارتباط با سرور برقرار نشد. مطمئن شوید برنامه در حال اجراست.', 'error', 'health');
    }
  }

  // ------------------------------------------------------------ form basics
  function updateCounter() {
    els.counter.textContent = `${toFa(els.topic.value.length)} / ${toFa(500)}`;
  }
  els.topic.addEventListener('input', () => { updateCounter(); els.topicError.hidden = true; });
  document.querySelectorAll('.chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      els.topic.value = chip.dataset.topic;
      updateCounter();
      els.topic.focus();
    });
  });
  updateCounter();

  // ------------------------------------------------------------ safe markdown renderer
  const ESC_TOKEN = /\u0000(\d+)\u0000/g;

  function inlineMd(src) {
    const tokens = [];
    const stash = (html) => { tokens.push(html); return `\u0000${tokens.length - 1}\u0000`; };
    let s = src;
    s = s.replace(/`([^`]+)`/g, (_, c) => stash(`<code>${c}</code>`));
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => stash(
      `<a href="${url}" target="_blank" rel="noopener noreferrer">${label.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')}</a>`));
    s = s.replace(/(^|[\s(،؛])(https?:\/\/[^\s<]*[^\s<.,;:!?)،؛])/g, (_, pre, url) => pre + stash(
      `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`));
    s = s.replace(/\[([0-9۰-۹]{1,3})\](?!\()/g, (_, d) => {
      const n = parseInt(d.replace(/[۰-۹]/g, (c) => FA.indexOf(c)), 10);
      return `<a class="cite" href="#src-${n}" data-cite="${n}" aria-label="منبع ${toFa(n)}">[${toFa(n)}]</a>`;
    });
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\*)/g, '$1<em>$2</em>');
    for (let i = 0; i < 4 && ESC_TOKEN.test(s); i += 1) {
      ESC_TOKEN.lastIndex = 0;
      s = s.replace(ESC_TOKEN, (_, idx) => tokens[Number(idx)]);
    }
    ESC_TOKEN.lastIndex = 0;
    return s;
  }

  const RE = {
    fence: /^```/,
    heading: /^(#{1,6})\s+(.*?)\s*#*\s*$/,
    hr: /^(-{3,}|\*{3,}|_{3,})\s*$/,
    quote: /^&gt;\s?(.*)$/,
    ul: /^\s*[-*+]\s+(.*)$/,
    ol: /^\s*(?:\d+|[۰-۹]+)[.)]\s+(.*)$/,
    tableSep: /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/,
  };

  function splitRow(line) {
    let t = line.trim();
    if (t.startsWith('|')) t = t.slice(1);
    if (t.endsWith('|')) t = t.slice(0, -1);
    return t.split('|').map((c) => c.trim());
  }

  function isBlockStart(line, next) {
    return RE.fence.test(line) || RE.heading.test(line) || RE.hr.test(line) || RE.quote.test(line)
      || RE.ul.test(line) || RE.ol.test(line)
      || (line.includes('|') && next !== undefined && RE.tableSep.test(next) && next.includes('-'));
  }

  // Escape FIRST, then convert: no raw HTML from the model/web can ever reach innerHTML.
  function renderMarkdown(md) {
    const lines = escapeHtml(String(md).replace(/\r\n?/g, '\n')).split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }

      if (RE.fence.test(line)) {
        const code = [];
        i += 1;
        while (i < lines.length && !RE.fence.test(lines[i])) { code.push(lines[i]); i += 1; }
        i += 1;
        out.push(`<pre><code>${code.join('\n')}</code></pre>`);
        continue;
      }
      let m = line.match(RE.heading);
      if (m) { out.push(`<h${m[1].length}>${inlineMd(m[2])}</h${m[1].length}>`); i += 1; continue; }
      if (RE.hr.test(line)) { out.push('<hr>'); i += 1; continue; }

      if (line.includes('|') && i + 1 < lines.length && RE.tableSep.test(lines[i + 1]) && lines[i + 1].includes('-')) {
        const head = splitRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { rows.push(splitRow(lines[i])); i += 1; }
        const th = head.map((c) => `<th>${inlineMd(c)}</th>`).join('');
        const tb = rows.map((r) => `<tr>${r.map((c) => `<td>${inlineMd(c)}</td>`).join('')}</tr>`).join('');
        out.push(`<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table></div>`);
        continue;
      }
      if (RE.quote.test(line)) {
        const parts = [];
        while (i < lines.length && RE.quote.test(lines[i])) { parts.push(lines[i].match(RE.quote)[1]); i += 1; }
        out.push(`<blockquote>${inlineMd(parts.join(' '))}</blockquote>`);
        continue;
      }
      if (RE.ul.test(line) || RE.ol.test(line)) {
        const ordered = RE.ol.test(line);
        const re = ordered ? RE.ol : RE.ul;
        const items = [];
        while (i < lines.length && re.test(lines[i])) { items.push(lines[i].match(re)[1]); i += 1; }
        const tag = ordered ? 'ol' : 'ul';
        out.push(`<${tag}>${items.map((t) => `<li>${inlineMd(t)}</li>`).join('')}</${tag}>`);
        continue;
      }
      const para = [];
      while (i < lines.length && lines[i].trim() && !(para.length && isBlockStart(lines[i], lines[i + 1]))) {
        para.push(lines[i].trim());
        i += 1;
      }
      out.push(`<p>${inlineMd(para.join(' '))}</p>`);
    }
    return out.join('\n');
  }

  function postProcessReport(root) {
    // Latin digits -> Persian in visible text (not in code or URL-like link text).
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      const parent = node.parentElement;
      if (!parent || parent.closest('code, pre')) return;
      if (parent.tagName === 'A' && /^https?:/i.test(node.nodeValue.trim())) return;
      node.nodeValue = faSmart(node.nodeValue);
    });
    // Give the sources list stable ids so [۱] citations can jump to them.
    root.querySelectorAll('h2').forEach((heading) => {
      if (!heading.textContent.includes('منابع')) return;
      const list = heading.nextElementSibling;
      if (list && list.tagName === 'OL') list.querySelectorAll('li').forEach((li, idx) => { li.id = `src-${idx + 1}`; });
    });
  }

  els.report.addEventListener('click', (ev) => {
    const cite = ev.target.closest('a.cite');
    if (!cite) return;
    ev.preventDefault();
    const target = document.getElementById(`src-${cite.dataset.cite}`);
    if (!target) return;
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    target.classList.add('flash');
    setTimeout(() => target.classList.remove('flash'), 1600);
  });

  // ------------------------------------------------------------ progress UI
  function setProgress(p) {
    const value = Math.max(0, Math.min(100, Number(p) || 0));
    els.barFill.style.width = `${value}%`;
    els.bar.setAttribute('aria-valuenow', String(Math.round(value)));
    els.progressLabel.textContent = `${toFa(Math.round(value))}٪`;
  }
  function setStageState(li, st) {
    li.dataset.state = st;
    li.querySelector('.stage-state').textContent = STAGE_LABELS[st];
  }
  function setStage(stage, failed) {
    state.stage = stage;
    const cur = STAGES.indexOf(stage);
    els.stages.forEach((li, idx) => {
      if (idx < cur) setStageState(li, 'done');
      else if (idx === cur) setStageState(li, failed ? 'failed' : 'running');
      else setStageState(li, 'pending');
    });
  }
  function addActivity(ev) {
    const time = new Date((ev.ts || Date.now() / 1000) * 1000)
      .toLocaleTimeString('fa-IR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const li = h('li', { 'data-type': ev.type, text: `${time} — ${faSmart(ev.message)}` });
    els.activity.append(li);
    while (els.activity.children.length > 250) els.activity.firstElementChild.remove();
    els.activity.scrollTop = els.activity.scrollHeight;
  }
  function domainOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ''); } catch (e) { return url; }
  }
  function addFinding(f) {
    const badge = h('span', { class: 'badge', text: BADGES[f.status] || BADGES.pending });
    const src = h('a', { class: 'finding-src', href: f.source_url, target: '_blank', rel: 'noopener noreferrer', text: domainOf(f.source_url) });
    const card = h('article', { class: 'finding', 'data-status': f.status || 'pending', 'data-id': String(f.id) },
      h('div', { class: 'finding-top' }, badge, src),
      h('p', { text: f.claim }),
      h('details', {}, h('summary', { text: 'شاهد' }), h('span', { text: f.evidence })),
      h('p', { class: 'reason' }));
    els.findings.append(card);
    state.findings.set(f.id, card);
    els.findingsCount.textContent = `(${toFa(state.findings.size)})`;
  }
  function updateFinding(d) {
    const card = state.findings.get(d.id);
    if (!card) return;
    card.dataset.status = d.status;
    card.querySelector('.badge').textContent = BADGES[d.status] || '';
    const conf = typeof d.confidence === 'number' ? ` (اطمینان ${toFa(Math.round(d.confidence * 100))}٪)` : '';
    card.querySelector('.reason').textContent = d.reason ? `${faSmart(d.reason)}${conf}` : conf;
  }

  // ------------------------------------------------------------ report display
  function showReport(md, id) {
    state.reportMd = md;
    state.reportId = id;
    els.report.innerHTML = renderMarkdown(md);
    postProcessReport(els.report);
    els.downloadBtn.href = `/api/jobs/${encodeURIComponent(id)}/export?format=md`;
    els.reportSection.hidden = false;
    els.reportSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  els.copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(state.reportMd);
    } catch (e) {
      const ta = h('textarea', { 'aria-hidden': 'true', style: 'position:fixed;opacity:0' });
      ta.value = state.reportMd;
      document.body.append(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (e2) { /* ignore */ }
      ta.remove();
    }
    toast('گزارش کپی شد.');
  });
  els.printBtn.addEventListener('click', () => window.print());

  // ------------------------------------------------------------ job flow
  function setRunning(running) {
    state.finished = !running;
    els.startBtn.disabled = running;
    els.stopBtn.hidden = !running;
    els.stopBtn.disabled = false;
  }

  function resetProgressUi() {
    setProgress(0);
    els.activity.replaceChildren();
    els.findings.replaceChildren();
    els.findingsCount.textContent = '';
    state.findings = new Map();
    els.stages.forEach((li) => setStageState(li, 'pending'));
    els.reportSection.hidden = true;
    els.progressSection.hidden = false;
  }

  function closeStream() {
    if (state.es) { state.es.close(); state.es = null; }
  }

  function finishJob() {
    closeStream();
    setRunning(false);
    loadHistory();
  }

  function handleEvent(ev) {
    if (typeof ev.seq === 'number') {
      if (ev.seq <= state.lastSeq) return; // duplicate after a reconnect
      state.lastSeq = ev.seq;
    }
    const data = ev.data || {};
    clearNotice('conn');
    setProgress(ev.progress);
    if (ev.stage && ev.type !== 'error' && ev.type !== 'cancelled') setStage(ev.stage, false);
    if (ev.type !== 'finding') addActivity(ev);

    switch (ev.type) {
      case 'finding':
        if (data.finding) addFinding(data.finding);
        addActivity(ev);
        break;
      case 'verification':
        if (typeof data.id === 'number') updateFinding(data);
        break;
      case 'complete':
        els.stages.forEach((li) => setStageState(li, 'done'));
        setProgress(100);
        if (data.partial) showNotice('پژوهش کامل نشد؛ گزارش جزئی ساخته شد.', 'warn', 'job');
        showReport(data.report, data.report_id);
        finishJob();
        break;
      case 'error':
        setStage(state.stage, true);
        showNotice(ev.message, 'error', 'job');
        finishJob();
        break;
      case 'cancelled':
        if (data.final) {
          showNotice('پژوهش متوقف شد و یافته‌ی قابل‌استفاده‌ای برای گزارش وجود نداشت.', 'warn', 'job');
          finishJob();
        }
        break;
      default:
        break;
    }
  }

  function connect() {
    closeStream();
    const es = new EventSource(`/api/jobs/${encodeURIComponent(state.jobId)}/events`);
    state.es = es;
    es.onopen = () => { state.retries = 0; clearNotice('conn'); };
    es.onmessage = (msg) => {
      let ev;
      try { ev = JSON.parse(msg.data); } catch (e) { return; }
      handleEvent(ev);
    };
    es.onerror = () => {
      if (state.finished) { es.close(); return; }
      showNotice('ارتباط با سرور قطع شد؛ در حال تلاش مجدد…', 'warn', 'conn');
      if (es.readyState === EventSource.CLOSED) {
        es.close();
        if (state.retries < 5) {
          state.retries += 1;
          setTimeout(() => { if (!state.finished) connect(); }, 1000 * state.retries);
        } else {
          showNotice('اتصال به سرور برقرار نشد. صفحه را تازه کنید و دوباره امتحان کنید.', 'error', 'job');
          finishJob();
        }
      }
    };
  }

  async function readDetail(res) {
    try {
      const body = await res.json();
      if (typeof body.detail === 'string') return body.detail;
    } catch (e) { /* ignore */ }
    return 'درخواست ناموفق بود. دوباره امتحان کنید.';
  }

  els.form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const topic = els.topic.value.replace(/\s+/g, ' ').trim();
    if (topic.length < 3 || topic.length > 500) {
      els.topicError.textContent = 'موضوع باید بین ۳ تا ۵۰۰ نویسه باشد.';
      els.topicError.hidden = false;
      els.topic.focus();
      return;
    }
    els.topicError.hidden = true;
    clearNotice();
    const depth = (document.querySelector('input[name="depth"]:checked') || {}).value || 'standard';
    els.startBtn.disabled = true;
    let res;
    try {
      res = await fetch('/api/jobs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ topic, depth }),
      });
    } catch (e) {
      showNotice('ارتباط با سرور برقرار نشد. مطمئن شوید برنامه در حال اجراست.', 'error', 'job');
      els.startBtn.disabled = false;
      return;
    }
    if (!res.ok) {
      showNotice(await readDetail(res), 'error', 'job');
      els.startBtn.disabled = false;
      return;
    }
    const body = await res.json();
    state.jobId = body.job_id;
    state.lastSeq = -1;
    state.retries = 0;
    state.stage = 'planning';
    resetProgressUi();
    setRunning(true);
    setStage('planning', false);
    els.progressSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    connect();
  });

  els.stopBtn.addEventListener('click', async () => {
    if (!state.jobId) return;
    els.stopBtn.disabled = true;
    try {
      await fetch(`/api/jobs/${encodeURIComponent(state.jobId)}/cancel`, { method: 'POST' });
      showNotice('درخواست توقف ارسال شد؛ گزارش جزئی از یافته‌های تأییدشده ساخته می‌شود…', 'warn', 'conn');
    } catch (e) {
      showNotice('ارسال درخواست توقف ناموفق بود.', 'error', 'job');
      els.stopBtn.disabled = false;
    }
  });

  // ------------------------------------------------------------ history
  function formatDate(iso) {
    try {
      return new Intl.DateTimeFormat('fa-IR-u-ca-persian', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(iso));
    } catch (e) { return iso; }
  }

  async function loadHistory() {
    let items = [];
    try {
      const res = await fetch('/api/history', { cache: 'no-store' });
      if (res.ok) items = await res.json();
    } catch (e) { /* server down: keep empty */ }
    els.historyList.replaceChildren();
    els.historyEmpty.hidden = items.length > 0;
    items.forEach((item) => {
      const meta = `${formatDate(item.created_at)}${item.partial ? ' • جزئی' : ''}`;
      const open = h('button', { class: 'history-open', type: 'button', title: item.topic },
        h('span', { class: 'h-topic', text: item.topic }), h('span', { class: 'h-meta', text: meta }));
      const del = h('button', { class: 'history-del', type: 'button', 'aria-label': `حذف پژوهش «${item.topic}»`, text: '✕' });
      const li = h('li', { class: 'history-item', 'data-active': String(item.id === state.reportId) }, open, del);
      open.addEventListener('click', () => openHistory(item.id));
      del.addEventListener('click', () => deleteHistory(item.id));
      els.historyList.append(li);
    });
  }

  async function openHistory(id) {
    try {
      const res = await fetch(`/api/history/${encodeURIComponent(id)}`);
      if (!res.ok) { showNotice(await readDetail(res), 'error', 'job'); return; }
      const item = await res.json();
      if (!state.finished) { showNotice('ابتدا پژوهش در حال اجرا را متوقف کنید یا صبر کنید.', 'warn', 'job'); return; }
      clearNotice();
      els.progressSection.hidden = true;
      showReport(item.report, item.id);
      closeDrawer();
      loadHistory();
    } catch (e) {
      showNotice('بازکردن گزارش ناموفق بود.', 'error', 'job');
    }
  }

  async function deleteHistory(id) {
    if (!window.confirm('این پژوهش برای همیشه حذف شود؟')) return;
    try {
      const res = await fetch(`/api/history/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (!res.ok) { showNotice(await readDetail(res), 'error', 'job'); return; }
    } catch (e) {
      showNotice('حذف ناموفق بود.', 'error', 'job');
      return;
    }
    if (state.reportId === id) { els.reportSection.hidden = true; state.reportId = null; }
    loadHistory();
  }

  $('#new-research').addEventListener('click', () => {
    if (!state.finished) return;
    els.reportSection.hidden = true;
    els.progressSection.hidden = true;
    state.reportId = null;
    closeDrawer();
    els.topic.focus();
    window.scrollTo({ top: 0, behavior: 'smooth' });
    loadHistory();
  });

  // ------------------------------------------------------------ mobile drawer
  function openDrawer() {
    els.sidebar.dataset.open = 'true';
    els.overlay.hidden = false;
    els.historyToggle.setAttribute('aria-expanded', 'true');
  }
  function closeDrawer() {
    els.sidebar.dataset.open = 'false';
    els.overlay.hidden = true;
    els.historyToggle.setAttribute('aria-expanded', 'false');
  }
  els.historyToggle.addEventListener('click', () => (els.sidebar.dataset.open === 'true' ? closeDrawer() : openDrawer()));
  els.overlay.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') closeDrawer(); });

  // ------------------------------------------------------------ boot
  setRunning(false);
  els.stopBtn.hidden = true;
  loadHistory();
  checkHealth();
  setInterval(() => { if (state.finished) checkHealth(); }, 30000);
})();
