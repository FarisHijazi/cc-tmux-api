// HQ frontend. API assumptions: docs/mobile-control-research.md and the mobile
// session contract. Actions are never queued or retried. Only shell assets cache.
import { h, render } from './vendor/preact.mjs';
import { useState, useEffect, useRef, useMemo } from './vendor/hooks.mjs';
import htm from './vendor/htm.mjs';
// Upstream distributions: marked 18.1.0 (marked.js.org), DOMPurify 3.4.16
// (github.com/cure53/DOMPurify). Licenses are alongside the vendored modules.
import { marked } from './vendor/marked.mjs';
import DOMPurify from './vendor/dompurify.mjs';
const html = htm.bind(h);
function requestId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  // getRandomValues remains available on direct HTTP origins.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
const paths = {
  inbox: 'M4 4h16v16H4z M4 13h5l2 3h2l2-3h5',
  workers: 'M8 4h8v4H8z M4 12h6v8H4z M14 12h6v8h-6z M12 8v2 M7 10h10',
  routines: 'M4 10a8 8 0 1 1 1 8 M4 4v6h6 M12 7v5l3 2',
  settings: 'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',
  search: 'M10 17a7 7 0 1 0 0-14 7 7 0 0 0 0 14 M15 15l6 6',
  arrow: 'M12 19V5 M6 11l6-6 6 6',
  back: 'M15 5l-7 7 7 7',
  refresh: 'M20 10a8 8 0 1 0-1 8 M20 4v6h-6',
  chevron: 'M9 5l7 7-7 7',
  terminal: 'M4 6l6 6-6 6 M12 18h8',
  check: 'M5 12l4 4L19 6',
  bell: 'M6 9a6 6 0 0 1 12 0v6l2 2H4l2-2z M10 21h4',
};
function Icon({ name, size = 20 }) {
  return html`<svg width=${size} height=${size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d=${paths[name] || paths.inbox}/></svg>`;
}
async function api(path, body, method = body === undefined ? 'GET' : 'POST') {
  const res = await fetch(path, { method, credentials: 'same-origin', cache: 'no-store',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    if (res.status === 401) window.dispatchEvent(new Event('hq-auth-required'));
    const detail = data.detail;
    throw new Error(typeof detail === 'string' ? detail : detail?.message || data.output || data.message || `Request failed (${res.status}).`);
  }
  return data;
}
function useResource(path, interval = 0) {
  const [state, setState] = useState({ data: null, error: '', hasLoaded: false, loading: true });
  const generation = useRef(0);
  const active = useRef(true);
  const reload = async () => {
    if (!path) return;
    const run = ++generation.current;
    setState(s => ({ ...s, loading: true }));
    try {
      const data = await api(path);
      if (active.current && run === generation.current) setState({ data, error: '', hasLoaded: true, loading: false });
      return data;
    } catch (e) {
      if (active.current && run === generation.current) setState(s => ({ ...s, error: e.message, hasLoaded: true, loading: false }));
      return null;
    }
  };
  useEffect(() => {
    active.current = true;
    setState({ data: null, error: '', hasLoaded: false, loading: true });
    reload();
    const timer = interval && setInterval(() => { if (!document.hidden) reload(); }, interval);
    const resume = () => { if (!document.hidden) reload(); };
    window.addEventListener('online', resume);
    document.addEventListener('visibilitychange', resume);
    return () => { active.current = false; generation.current++; clearInterval(timer); window.removeEventListener('online', resume); document.removeEventListener('visibilitychange', resume); };
  }, [path]);
  return { ...state, reload };
}
function ErrorNotice({ message, retry }) {
  return message && html`<div class="error" role="alert"><span>${message}</span>${retry && html`<button onClick=${retry}>Retry</button>`}</div>`;
}
function Loading({ label = 'Loading sessions' }) {
  return html`<div class="loading" role="status"><span class="spinner"/>${label}…</div>`;
}
function Empty({ title, children, icon = 'inbox' }) {
  return html`<div class="empty"><div class="empty-icon"><${Icon} name=${icon} size=${28}/></div><h2>${title}</h2><p>${children}</p></div>`;
}
function lifecycle(session) {
  if (['alive','ended','unreachable'].includes(session?.lifecycle)) return session.lifecycle;
  if (session?.alive === true) return 'alive';
  if (session?.alive === false) return 'ended';
  if (session?.alive === null) return 'unreachable';
  // Compatibility with the running pre-lifecycle API, which still verifies
  // session revisions. Never infer liveness from an absent/loading record or
  // override explicit lifecycle, stale, or offline metadata.
  if (session && session.lifecycle == null && session.alive === undefined &&
      typeof session.revision === 'string' && session.revision.trim() &&
      !session.stale && session.online !== false && session.available !== false) return 'alive';
  return 'unknown';
}
const canControl = session => lifecycle(session) === 'alive';
const attention = s => canControl(s) && (!!s.menu || s.has_question === true || ['question','permission'].includes(s.attention_kind) || /waiting|question|menu|attention|blocked|error|failed|permission|accident/i.test([s.status, s.status_label].filter(Boolean).join(' ')));
function Lifecycle({ session }) {
  const state = lifecycle(session);
  return html`<span class=${`lifecycle ${state}`}>${{alive:'Alive',ended:'Ended',unreachable:'Unreachable',unknown:'Lifecycle unknown'}[state]}</span>`;
}
function ActionLabel({ session }) {
  if (!attention(session)) return null;
  const label = session.has_question === true || session.attention_kind === 'question' || session.menu || /question|menu/i.test(session.status || '') ? 'Question' : session.attention_kind === 'permission' || /permission/i.test(session.status_label || '') ? 'Needs permission' : 'Awaiting action';
  return html`<span class="action-label">${label}</span>`;
}
function Status({ session }) {
  return html`<span class=${`status ${attention(session) ? 'attention' : canControl(session) && /working|running|tool/.test(session.status) ? 'working' : ''}`}>${session.status_emoji ? html`<span class="status-emoji">${session.status_emoji}</span>` : html`<i aria-hidden="true"/>`}<span>${['ended','unreachable'].includes(lifecycle(session)) && html`<span class="status-history">Last activity: </span>`}${session.status_label || session.status || 'Unknown'}</span></span>`;
}
function when(value) {
  if (!value) return 'Time unavailable';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  if (isNaN(date)) return 'Time unavailable';
  const mins = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
  return mins < 1 ? 'Just now' : mins < 60 ? `${mins}m ago` : mins < 1440 ? `${Math.floor(mins / 60)}h ago` : date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}
function endedDate(value) {
  if (!value) return 'End time unavailable';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return isNaN(date) ? 'End time unavailable' : `Ended ${date.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}`;
}
// Federation coordinator entries are navigation ancestors, not extra machines.
// These helpers affect presentation only; IDs and request paths remain intact.
function leafHosts(hosts) {
  const path = host => host.host_path?.length ? host.host_path : [host.id];
  return hosts.filter(host => {
    const own = path(host);
    return !hosts.some(other => {
      const next = path(other);
      return next.length > own.length && own.every((part, i) => part === next[i]);
    });
  });
}
function displayHostLabel(label) {
  const parts = String(label || '').split(/\s*\/\s*/).filter(Boolean);
  const compact = parts.filter((part, i) => i === 0 || part.toLowerCase() !== parts[i - 1].toLowerCase());
  if (compact.length > 1 && compact.at(-1).toLowerCase() === 'local') compact.pop();
  return compact.join(' / ');
}
function readDraft(id) { try { return localStorage.getItem(`hq:draft:${id}`) || ''; } catch { return ''; } }
function saveDraft(id, text) { try { if (text) localStorage.setItem(`hq:draft:${id}`, text); else localStorage.removeItem(`hq:draft:${id}`); return true; } catch { return false; } }
function sessionHash() { return new URLSearchParams(location.hash.slice(1)).get('session'); }
function Inbox({ resource, selected, open }) {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const [group, setGroup] = useState('host');
  const sessions = resource.data?.sessions || [];
  const endedCount = sessions.filter(s => lifecycle(s) === 'ended').length;
  const activeCount = sessions.length - endedCount;
  const displayHosts = leafHosts(resource.data?.hosts || []);
  const unavailable = displayHosts.some(host => !host.available);
  const needing = sessions.filter(attention).length;
  const matching = sessions.filter(s => (!query || [s.title, s.host_label, s.workdir, s.session].join(' ').toLowerCase().includes(query.toLowerCase())) && (filter === 'all' || (filter === 'active' ? lifecycle(s) !== 'ended' : filter === 'ended' ? lifecycle(s) === 'ended' : filter === 'attention' ? attention(s) : canControl(s) && /working|running|tool/i.test(s.status))));
  const groups = new Map();
  matching.sort((a,b) => Number(attention(b)) - Number(attention(a)) || String(a.title || a.session).localeCompare(String(b.title || b.session))).forEach(s => {
    const key = group === 'host' ? s.host_label || s.host : s.workdir || 'Directory unavailable';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  });
  const ordered = [...groups].sort((a,b) => Number(b[1].some(attention)) - Number(a[1].some(attention)));
  return html`<section class="inbox" aria-label="Session inbox">
    <div class="inbox-heading"><div><div class="eyeline">Your workspace, within reach</div><h1>Sessions ${resource.data && !(unavailable && !sessions.length) && html`<span class="total">${filter === 'ended' ? endedCount : activeCount}<small>${filter === 'ended' ? 'ended' : 'active'}</small></span>`}</h1></div><button class="icon-button" aria-label="Refresh sessions" onClick=${resource.reload} disabled=${resource.loading}><${Icon} name="refresh"/></button></div>
    <div class="inbox-tools"><label class="search"><${Icon} name="search"/><input type="search" aria-label="Search sessions" placeholder="Search sessions, hosts, folders" value=${query} onInput=${e => setQuery(e.target.value)}/></label>
    <div class="filters" aria-label="Filter sessions">${[['all','All sessions'],['active','Active'],['ended','Ended'],['attention','Needs you'],['working','Working']].map(([id,label]) => html`<button class=${filter === id ? 'active' : ''} aria-pressed=${filter === id} onClick=${() => setFilter(id)}>${label}${id === 'attention' && resource.data && needing > 0 && html`<span class="filter-count">${needing}</span>`}</button>`)}</div>
    <div class="group-control"><span>Group by</span><div class="segmented">${['host','directory'].map(g => html`<button aria-pressed=${group === g} class=${group === g ? 'active' : ''} onClick=${() => setGroup(g)}>${g === 'host' ? 'Host' : 'Directory'}</button>`)}</div></div></div>
    <div class="inbox-scroll"><${ErrorNotice} message=${resource.error} retry=${resource.reload}/>
    ${displayHosts.filter(host => !host.available).map(host => html`<div class="host-error" role="status"><strong>${displayHostLabel(host.label)} is unavailable</strong><span>${host.error || 'Reconnect this host to refresh its sessions.'}</span></div>`)}
    ${!resource.hasLoaded ? html`<${Loading}/>` : resource.data && !matching.length ? unavailable ? html`<${Empty} title="Waiting for hosts">Some hosts cannot be reached, so sessions may be missing from this view. HQ will keep checking.<button onClick=${resource.reload}>Refresh hosts</button><//>` : html`<${Empty} title=${query || filter !== 'all' ? 'No matching sessions' : 'A clear workspace'}>${query || filter !== 'all' ? 'Try another search or filter.' : 'Sessions from your connected hosts will appear here.'}<//>` : null}
    ${ordered.map(([name, rows]) => html`<section class="session-group"><h2 title=${name}>${group === 'host' ? displayHostLabel(name) : name}<span>${rows.length}</span></h2>${rows.map(s => html`<button class=${`session-row ${s.id === selected ? 'selected' : ''}`} onClick=${() => open(s.id)} aria-current=${s.id === selected ? 'true' : undefined}><span class=${`session-mark ${attention(s) ? 'needs-you' : ''}`}><${Icon} name=${attention(s) ? 'bell' : 'terminal'} size=${19}/></span><span class="session-main"><span class="row-title">${s.title || s.session}</span><span class="row-directory" dir="auto">${group === 'host' ? s.workdir || s.session : displayHostLabel(s.host_label || s.host)}</span><span class="row-lifecycle"><${Lifecycle} session=${s}/><${ActionLabel} session=${s}/></span><span class="row-bottom"><${Status} session=${s}/><span class="row-time">${lifecycle(s) === 'ended' ? endedDate(s.ended_at) : lifecycle(s) === 'unreachable' ? `Last seen ${when(s.last_seen)}` : when(s.updated_at)}</span></span></span><${Icon} name="chevron" size=${16}/></button>`)}</section>`)}
    </div><div class="inbox-footer">${resource.data ? `${displayHosts.filter(h => h.available).length} of ${displayHosts.length} hosts connected` : 'Connecting to your workspace'}<span>${resource.loading && resource.hasLoaded ? 'Refreshing…' : 'HQ'}</span></div>
  </section>`;
}
// Marked parses; DOMPurify is the only path from transcript HTML into the DOM.
// No media, styles, event attributes, named properties, or custom elements.
const markdownPolicy = {
  ALLOWED_TAGS: ['p','br','hr','h1','h2','h3','h4','h5','h6','ul','ol','li','blockquote','pre','code','kbd','strong','b','em','i','s','del','a','table','thead','tbody','tfoot','tr','th','td'],
  ALLOWED_ATTR: ['href','title','start','colspan','rowspan'],
  ALLOW_DATA_ATTR:false, ALLOW_ARIA_ATTR:false, SANITIZE_NAMED_PROPS:true,
};
DOMPurify.addHook('uponSanitizeAttribute', (_node, data) => {
  if (data.attrName !== 'href') return;
  try { if (!['http:','https:','mailto:'].includes(new URL(data.attrValue, location.href).protocol)) data.keepAttr = false; }
  catch { data.keepAttr = false; }
});
function MessageText({ text }) {
  const clean = useMemo(() => DOMPurify.sanitize(marked.parse(String(text || ''), {gfm:true,breaks:false}), markdownPolicy), [text]);
  return html`<div class="message-text markdown" dir="auto" dangerouslySetInnerHTML=${{__html:clean}}/>`;
}
function TranscriptEmpty({ session, openTerminal }) {
  const reason = {
    identity_missing: 'HQ couldn’t identify the exact Claude conversation for this session.',
    file_missing: 'Claude hasn’t saved a conversation file for this session.',
    parser_empty: 'Claude’s conversation file has no readable messages yet.',
    available: 'No conversation messages are available yet.',
  }[session.transcript_status] || 'No conversation history is available for this session.';
  const live = canControl(session);
  return html`<div class="transcript-empty"><${Empty} title="Conversation not available" icon="terminal">${reason} ${live ? 'You can still use the live terminal below.' : 'Any last known terminal output is shown below.'}<button onClick=${openTerminal}>Open terminal</button><//><pre class="terminal-output">${session.tail || (live ? 'No terminal output is available yet.' : 'No last known terminal output is available.')}</pre></div>`;
}
// fleet_probe.parse_menu owns option kinds. Label recognition is only a
// compatibility fallback for snapshots without the structured kind field.
// Selection commits a single choice or toggles a checkbox. Review submission
// uses its own action; typing only inserts text and never presses Enter.
const isFreeChoice = option => option?.kind ? option.kind === 'free_text' : /^(?:type something(?: else)?|other|custom(?: answer)?|free[- ]text)[.… ]*$/i.test(option?.label || '');
function activeReviewSection(tail) {
  const lines = String(tail || '').split(/\r?\n/);
  const markers = lines.map(line => line.trim());
  const start = markers.lastIndexOf('Review your answers');
  if (start < 0) return null;
  const end = markers.findIndex((line, index) => index > start && line === 'Ready to submit your answers?');
  return end < 0 ? null : lines.slice(start, end + 1).join('\n');
}
function Question({ id, menu, tail, disabled, act }) {
  // A screen signature can change when native input replaces its placeholder.
  // Keep drafts and confirmed custom-option identity stable within this question.
  const draftId = useRef(`${id}:answer:${menu.question}:${menu.kind}`).current;
  const customIndexes = useRef(new Set());
  menu.options.filter(isFreeChoice).forEach(option => customIndexes.current.add(option.index));
  const customChoice = option => !!option && (option.kind ? option.kind === 'free_text' : customIndexes.current.has(option.index));
  const [answer, setAnswer] = useState(() => readDraft(draftId));
  const [saved, setSaved] = useState(true);
  const [inserted, setInserted] = useState(null);
  const focused = menu.options.find(o => o.index === menu.focused_index);
  const review = menu.kind === 'review';
  const reviewSection = review ? activeReviewSection(tail) : null;
  const free = !review && customChoice(focused);
  const hasInserted = inserted !== null && inserted === focused?.index;
  const submitOption = menu.options.find(option => option.kind === 'submit' || option.kind === 'next');
  const confirm = () => review ? act('submit') : submitOption ? act('select', {index:submitOption.index}) : free ? act('key', {key:'Enter'}) : undefined;
  // Backend select focuses custom text without submitting or toggling it.
  const choose = option => act('select', { index:option.index });
  return html`<section class="question-card" aria-label="Pending question"><div class="question-label">${review ? 'Review your answer' : 'Your input is needed'}<span>${review ? 'Ready to submit' : menu.multi_select ? 'Multiple choice' : 'Single choice'}</span></div><h3>${menu.question || 'Choose an option in the terminal'}</h3>
    ${review && html`<div class="review-summary"><div class="message-author">Current answer review</div>${reviewSection ? html`<pre>${reviewSection}</pre>` : html`<p>Current review is unavailable. Open the Terminal tab to check your answers before submitting.</p>`}</div>`}
    <div class="question-options">${menu.options.filter(o => !['submit','next'].includes(o.kind)).map(o => html`<button class=${`question-option ${o.index === menu.focused_index ? 'focused' : ''} ${o.selected ? 'chosen' : ''}`} disabled=${disabled || review || !Number.isInteger(menu.focused_index)} aria-pressed=${menu.multi_select ? !!o.selected : undefined} onClick=${() => choose(o)}><span class=${`choice-mark ${menu.multi_select ? 'square' : ''}`}>${o.selected && html`<${Icon} name="check" size=${14}/>`}</span><span><strong>${o.label}</strong>${o.description && html`<small>${o.description}</small>`}</span>${o.index === menu.focused_index && html`<span class="focus-label">Focused</span>`}</button>`)}</div>
    <p class="question-help">${review ? 'Check your answers, then submit them together.' : menu.multi_select ? 'Tap to toggle a choice. Review when your selection is ready.' : 'Tap a choice to send it directly.'}</p>
    ${free && html`<form class="answer-form" onSubmit=${async e => { e.preventDefault(); if (disabled || !answer.trim()) return; if (await act('type', {text:answer})) { setAnswer(''); saveDraft(draftId, ''); setInserted(focused.index); } }}><label for="custom-answer">Your answer</label><textarea id="custom-answer" rows="2" value=${answer} disabled=${disabled} onInput=${e => { setAnswer(e.target.value); setSaved(saveDraft(draftId, e.target.value)); }} placeholder="Type your own answer…"/><div class="answer-footer"><span role="status">${hasInserted ? 'Text inserted. Confirm when ready.' : !saved ? 'Draft could not be saved on this device.' : 'Inserts text without submitting.'}</span><button disabled=${disabled || !answer.trim()}>Insert answer</button></div></form>`}
    <div class="actions"><button disabled=${disabled} onClick=${() => act('key', {key:'Escape'})}>Cancel</button>${(review || menu.multi_select || free) && html`<button class="primary" disabled=${disabled || (free && (!hasInserted || !!answer.trim())) || (!review && !free && !submitOption)} onClick=${confirm}>${review ? 'Submit answer' : free ? 'Confirm typed answer' : 'Continue'}</button>`}</div>
  </section>`;
}
function Detail({ id, snapshot, back, refreshList, muted, toggleMute }) {
  const resource = useResource(`/api/sessions/${encodeURIComponent(id)}`, 5000);
  // Retain only this mounted session's already-viewed history, never persist it.
  const history = useRef({messages:null,tail:''});
  if (resource.data?.messages?.length) history.current.messages = resource.data.messages;
  if (resource.data?.tail) history.current.tail = resource.data.tail;
  // Unreachable hosts may return an HTTP error for detail. The index still
  // carries authoritative lifecycle; never keep showing cached Alive controls.
  const observed = snapshot && ['ended','unreachable'].includes(lifecycle(snapshot)) ? {...resource.data,...snapshot} : resource.data || snapshot;
  const s = observed ? {...observed, messages:observed.messages?.length ? observed.messages : history.current.messages, tail:observed.tail || history.current.tail} : null;
  const [tab, setTab] = useState('conversation');
  const [draft, setDraft] = useState(() => readDraft(id));
  const [draftOpen, setDraftOpen] = useState(false);
  const [saved, setSaved] = useState(true);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [notice, setNotice] = useState('');
  const lock = useRef(false);
  const transcript = useRef(null);
  const pinned = useRef(true);
  const shownQuestion = useRef(null);
  useEffect(() => {
    const container = transcript.current;
    if (!container) return;
    const question = s?.menu ? s.menu.signature || JSON.stringify([s.menu.question, s.menu.kind]) : null;
    if (question) {
      if (shownQuestion.current !== question) {
        const card = container.querySelector('.question-card');
        if (card) container.scrollTop += card.getBoundingClientRect().top - container.getBoundingClientRect().top - 12;
        shownQuestion.current = question;
        setDraftOpen(false);
      }
      return;
    }
    shownQuestion.current = null;
    if (pinned.current) container.scrollTop = container.scrollHeight;
  }, [s?.messages, s?.menu, tab]);
  const act = async (action, extra = {}) => {
    if (lock.current || !resource.data || !canControl(s) || s?.revision == null || resource.error || !navigator.onLine) return false;
    lock.current = true; setBusy(true); setActionError(''); setNotice('');
    try {
      await api(`/api/sessions/${encodeURIComponent(id)}/action`, { action, ...extra, revision: s.revision, request_id: requestId() });
      if (action === 'send') { setDraft(''); saveDraft(id, ''); }
      setNotice(action === 'send' ? 'Message sent.' : 'Control sent. Screen refreshed.');
      return true;
    } catch (e) { setActionError(`${e.message} Nothing will be retried automatically. Check the refreshed session before trying again.`); return false; }
    finally { await resource.reload(); refreshList(); lock.current = false; setBusy(false); }
  };
  const disabled = busy || !resource.data || !canControl(s) || !s || s.revision == null || !!resource.error || !navigator.onLine;
  const key = value => act('key', { key: value });
  return html`<section class="detail" aria-label="Session detail"><header class="detail-header"><button class="icon-button back" aria-label="Back to sessions" onClick=${back}><${Icon} name="back"/></button><div class="detail-title"><h2>${s?.title || s?.session || 'Session'}</h2><span>${s ? displayHostLabel(s.host_label || s.host) : 'Connecting…'}</span></div>${s && html`<${Status} session=${s}/>`}<button class="icon-button" disabled=${resource.loading} aria-label="Refresh session" onClick=${resource.reload}><${Icon} name="refresh"/></button></header>
    ${s && html`<div class="detail-context"><span title=${s.workdir} dir="auto">${s.workdir || s.session}</span>${toggleMute && html`<button class="quiet-button" onClick=${() => toggleMute(id)}>${muted ? 'Unmute' : 'Mute'}</button>`}</div>`}
    ${s && html`<div class="detail-lifecycle"><${Lifecycle} session=${s}/><${ActionLabel} session=${s}/><span>${lifecycle(s) === 'ended' ? endedDate(s.ended_at) : lifecycle(s) === 'unreachable' ? `Last seen ${when(s.last_seen)}` : ''}</span></div>`}
    ${s && !canControl(s) && html`<div class="lifecycle-notice" role="status">${lifecycle(s) === 'ended' ? 'This session has ended. Its available history is read-only.' : lifecycle(s) === 'unreachable' ? 'This session is unreachable. Its last known activity does not mean it has ended.' : 'Session availability has not been confirmed. Controls are unavailable.'}</div>`}
    <div class="detail-tabs" role="tablist" aria-label="Session view">${['conversation','terminal'].map(t => html`<button role="tab" id=${`tab-${t}`} aria-selected=${tab === t} aria-controls="session-panel" class=${tab === t ? 'active' : ''} onClick=${() => setTab(t)}>${t === 'conversation' ? 'Conversation' : 'Terminal'}</button>`)}</div>
    <${ErrorNotice} message=${resource.error} retry=${resource.reload}/><${ErrorNotice} message=${actionError}/>
    <div class="transcript" id="session-panel" role="tabpanel" aria-labelledby=${`tab-${tab}`} ref=${transcript} onScroll=${e => { const el = e.currentTarget; pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100; }}>
      ${!resource.hasLoaded ? html`<${Loading} label="Opening session"/>` : s && tab === 'terminal' ? html`<pre class="terminal-output">${s.tail || (canControl(s) ? 'No terminal output is available yet.' : 'No last known terminal output is available.')}</pre>` : s && html`<div class="messages">${s.messages?.length ? s.messages.map((m, i) => html`<article class=${`message ${m.role}`} key=${i}><div class="message-author">${m.role === 'user' ? 'You' : 'Claude'}</div><${MessageText} text=${m.text}/></article>`) : html`<${TranscriptEmpty} session=${s} openTerminal=${() => setTab('terminal')}/>`}</div>`}
      ${s?.menu && canControl(s) && html`<${Question} key=${s.menu.question + s.menu.kind} id=${id} menu=${s.menu} tail=${s.tail} disabled=${disabled} act=${act}/>`}
    </div>
    <div class="detail-bottom">${s && !canControl(s) ? html`<p class="read-only-note">Read-only${draft ? ' · Your message draft is saved on this device.' : ''}</p>` : html`<div><details class="controls" open=${tab === 'terminal'}><summary>Keyboard controls</summary><div class="key-row">${[['Up','↑'],['Down','↓'],['Left','←'],['Right','→'],['Space','Space'],['Enter','Enter'],['Escape','Esc'],['Tab','Tab']].map(([value,label]) => html`<button disabled=${disabled} aria-label=${value} onClick=${() => key(value)}>${label}</button>`)}<${ArmedButton} label="Interrupt" disabled=${disabled} onFire=${() => key('C-c')}/></div></details>
    ${s?.menu && html`<button class="draft-toggle" aria-expanded=${draftOpen} aria-controls="session-composer" onClick=${() => setDraftOpen(open => !open)}>${draftOpen ? 'Hide draft' : 'Draft a message'}${draft && html`<span>Saved</span>`}</button>`}<div id="session-composer" hidden=${!!s?.menu && !draftOpen}><form class="composer" onSubmit=${e => { e.preventDefault(); if (draft.trim() && !s?.menu) act('send', { text: draft }); }}><label class="sr-only" for="reply">Message this session</label><textarea id="reply" rows="2" value=${draft} placeholder=${s?.menu ? 'Draft a message for after this question…' : 'Message this session…'} onInput=${e => { setDraft(e.target.value); setSaved(saveDraft(id, e.target.value)); }}/><button class="primary send-button" type="submit" aria-label="Send message" disabled=${disabled || !draft.trim() || !!s?.menu}><${Icon} name="arrow"/></button></form><div class="composer-meta" role="status">${busy ? 'Sending…' : s?.menu ? 'Use the question controls above. Your draft stays here.' : notice || (draft ? saved ? 'Draft saved on this device' : 'Draft could not be saved on this device' : 'Sends directly to the live session')}</div></div></div>`}</div>
  </section>`;
}
function ArmedButton({ label, onFire, disabled = false }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => { if (armed) { const timer = setTimeout(() => setArmed(false), 4000); return () => clearTimeout(timer); } }, [armed]);
  return html`<button type="button" disabled=${disabled} class=${armed ? 'danger' : ''} onClick=${() => { if (armed) { setArmed(false); onFire(); } else setArmed(true); }}>${armed ? `Confirm ${label.toLowerCase()}` : label}</button>`;
}
function Worker({ worker: w, action, busy }) {
  const [text, setText] = useState('');
  return html`<article class="worker-card" id=${`w-${w.session}`}><div class="worker-heading"><h2>${w.title || w.session}</h2><${Status} session=${w}/></div><div class="muted">${w.session}${w.attached ? ' · Attached' : ''}</div><details><summary>Terminal output</summary><pre class="terminal-output compact">${w.tail || 'No output yet.'}</pre></details><form class="actions" onSubmit=${async e => { e.preventDefault(); if (w.control && text.trim() && await action('/api/workers/action', { action:'send', session:w.session, text, ...w.control, request_id:requestId() })) setText(''); }}><input aria-label=${`Message ${w.title || w.session}`} placeholder="Steer this worker…" disabled=${!w.control} value=${text} onInput=${e => setText(e.target.value)}/><button disabled=${busy || !w.control || !text.trim()}>Send</button><${ArmedButton} label="Kill worker" disabled=${busy} onFire=${() => action('/api/workers/action', { action:'kill', session:w.session })}/></form>${!w.control && html`<p class="muted" role="status">No active controllable Claude session. Sending is unavailable.</p>`}${/^https?:\/\//.test(w.web_url || '') && html`<a href=${w.web_url} target="_blank" rel="noopener noreferrer">Open conversation ↗</a>`}</article>`;
}
function Operations({ view }) {
  const resource = useResource('/api/workers', 8000);
  const [title, setTitle] = useState('');
  const [prompt, setPrompt] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const lock = useRef(false);
  const action = async (path, body) => {
    if (lock.current) return false;
    lock.current = true; setBusy(true); setError(''); setNotice('');
    try { await api(path, body); setNotice('Action completed.'); await resource.reload(); return true; }
    catch(e) { setError(`${e.message} The action has not been retried.`); return false; }
    finally { lock.current = false; setBusy(false); }
  };
  const fmt = t => t ? new Date(t * 1000).toLocaleString([], { weekday:'short', hour:'2-digit', minute:'2-digit' }) : 'Not scheduled';
  return html`<section class="secondary"><div class="page-heading"><div><div class="eyeline">${view === 'workers' ? 'Delegated work' : 'Work on a schedule'}</div><h1>${view === 'workers' ? 'Workers' : 'Routines'}</h1></div><button class="icon-button" aria-label="Refresh" onClick=${resource.reload}><${Icon} name="refresh"/></button></div><${ErrorNotice} message=${resource.error} retry=${resource.reload}/><${ErrorNotice} message=${error}/>${notice && html`<p role="status">${notice}</p>`}
    ${!resource.hasLoaded ? html`<${Loading} label=${`Loading ${view}`}/>` : resource.data && (view === 'workers' ? html`${(resource.data.workers || []).map(w => html`<${Worker} key=${w.session} worker=${w} action=${action} busy=${busy}/>`)}${!resource.data.workers?.length && html`<${Empty} title="No workers running" icon="workers">Launch a worker to delegate a task.<//>`}<details class="launch-panel"><summary>Launch a worker</summary><form class="launch-form" onSubmit=${async e => { e.preventDefault(); if (title.trim() && prompt.trim() && await action('/api/workers/action', { action:'spawn', title, prompt })) { setTitle(''); setPrompt(''); } }}><label>Title<input required value=${title} onInput=${e => setTitle(e.target.value)}/></label><label>Task<textarea required rows="4" value=${prompt} onInput=${e => setPrompt(e.target.value)}/></label><p class="muted">Workers run unattended with permission checks bypassed.</p><button class="primary" disabled=${busy || !title.trim() || !prompt.trim()}>Launch worker</button></form></details>${resource.data.others?.length > 0 && html`<details class="other-sessions"><summary>Other tmux sessions (${resource.data.others.length})</summary>${resource.data.others.map(o => html`<div class="routine-row"><strong>${o.session}</strong><span class="muted">${o.command}${o.attached ? ' · Attached' : ''}</span></div>`)}</details>`}` : html`${(resource.data.routines || []).map(r => html`<article class="routine-row"><div><h2>${r.title || r.name}</h2><p>${r.every} ${(r.days || []).join(', ')}</p><span class="muted">Last: ${fmt(r.last_run)}<br/>${r.enabled ? `Next: ${fmt(r.next_run)}` : 'Schedule paused'}${r.running ? ` · Running: ${r.running}` : ''}</span></div><div class="actions"><button disabled=${busy} onClick=${() => action('/api/routines/action', { action:'run', name:r.name })}>Run now</button><button disabled=${busy} role="switch" aria-checked=${!!r.enabled} aria-label=${`Enable ${r.title || r.name}`} onClick=${() => action('/api/routines/action', { action:'toggle', name:r.name, enabled:!r.enabled })}>${r.enabled ? 'Enabled' : 'Paused'}</button></div></article>`)}${!resource.data.routines?.length && html`<${Empty} title="No routines configured" icon="routines">Your configured recurring tasks will appear here.<//>`}`)}
  </section>`;
}
function Settings({ auth, logout }) {
  const push = useResource('/api/push');
  const [subscription, setSubscription] = useState(null);
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const supported = window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  useEffect(() => { let alive = true; if (supported) navigator.serviceWorker.getRegistration('/').then(r => r?.pushManager.getSubscription()).then(s => { if (alive) { setSubscription(s || null); setChecked(true); } }).catch(e => { if (alive) { setError(e.message); setChecked(true); } }); else setChecked(true); return () => { alive = false; }; }, []);
  const enroll = async () => {
    setBusy(true); setError('');
    try {
      if (subscription) { await api('/api/push/subscribe', { endpoint:subscription.endpoint }, 'DELETE'); await subscription.unsubscribe(); setSubscription(null); }
      else {
        const permission = await Notification.requestPermission();
        if (permission !== 'granted') throw new Error('Notifications are not allowed. Enable them in your device settings to try again.');
        const registration = await navigator.serviceWorker.getRegistration('/');
        if (!registration?.active) throw new Error('The app is still preparing notifications. Reload and try again.');
        const encoded = push.data.public_key.replace(/-/g, '+').replace(/_/g, '/');
        const key = Uint8Array.from(atob(encoded + '='.repeat((4 - encoded.length % 4) % 4)), c => c.charCodeAt(0));
        const sub = await registration.pushManager.subscribe({ userVisibleOnly:true, applicationServerKey:key });
        try { await api('/api/push/subscribe', { subscription:sub.toJSON() }); } catch(e) { await sub.unsubscribe(); throw e; }
        setSubscription(sub);
      }
    } catch(e) { setError(e.message); } finally { setBusy(false); }
  };
  return html`<section class="secondary settings"><div class="page-heading"><div><div class="eyeline">Make yourself at home</div><h1>Settings</h1></div></div><section class="setting-block"><div class="setting-icon"><${Icon} name="bell"/></div><h2>Stay in the loop</h2><p>Get a notification when a session needs your attention.</p><${ErrorNotice} message=${push.error} retry=${push.reload}/><${ErrorNotice} message=${error}/>${!push.hasLoaded || !checked ? html`<${Loading} label="Checking notifications"/>` : !supported ? html`<p class="muted">${window.isSecureContext ? 'On iPhone, add HQ to your Home Screen from Safari’s Share menu, then open it there to enable notifications.' : 'Notifications are unavailable over HTTP. Sessions and controls still work.'}</p>` : push.data && !push.data.available ? html`<p class="muted">${push.data.reason || 'Notifications are not configured on this server.'}</p>` : push.data && html`<button class="primary" disabled=${busy} onClick=${enroll}>${busy ? 'Updating…' : subscription ? 'Turn off notifications' : 'Enable notifications'}</button>`}</section><section class="setting-block"><h2>On this device</h2><p>Appearance follows your system. Message drafts stay on this device until sent. Conversations are never stored for offline use.</p></section>${auth.required && html`<section class="setting-block"><h2>Paired with HQ</h2><p>This browser has access to your workspace.</p><button onClick=${logout}>Sign out</button></section>`}<p class="settings-brand">HQ <span>A little closer to your work.</span></p></section>`;
}
function Pairing({ paired }) {
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return html`<main class="pairing"><div class="brand-mark">hq<span/></div><h1>Your workspace.<br/>Wherever you are.</h1><p>Pair this device to pick up your sessions.</p><form onSubmit=${async e => { e.preventDefault(); if (busy || !token.trim()) return; setBusy(true); setError(''); try { await api('/api/auth/login', { token }); setToken(''); paired(); } catch(e) { setError(e.message); } finally { setBusy(false); } }}><label for="pair-token">Pairing token</label><input id="pair-token" type="password" autoComplete="off" autoCapitalize="none" spellCheck="false" required value=${token} onInput=${e => setToken(e.target.value)}/><${ErrorNotice} message=${error}/><button class="primary" disabled=${busy || !token.trim()}>${busy ? 'Pairing…' : 'Connect to HQ'}</button></form><small>Use the pairing token from your HQ server.</small></main>`;
}
function Workspace({ auth, logout }) {
  const [view, setView] = useState(location.hash.startsWith('#w-') ? 'workers' : 'inbox');
  const [selected, setSelected] = useState(sessionHash);
  const [online, setOnline] = useState(navigator.onLine);
  const sessions = useResource('/api/sessions', 8000);
  const preferences = useResource('/api/push/preferences');
  const [error, setError] = useState('');
  const muteLock = useRef(false);
  useEffect(() => {
    const hash = () => { setSelected(sessionHash()); if (sessionHash()) setView('inbox'); };
    const connection = () => setOnline(navigator.onLine);
    window.addEventListener('hashchange', hash); window.addEventListener('online', connection); window.addEventListener('offline', connection);
    return () => { window.removeEventListener('hashchange', hash); window.removeEventListener('online', connection); window.removeEventListener('offline', connection); };
  }, []);
  useEffect(() => {
    const viewport = window.visualViewport;
    const resize = () => { document.documentElement.style.setProperty('--app-height', `${viewport?.height || window.innerHeight}px`); };
    resize(); viewport?.addEventListener('resize', resize); window.addEventListener('resize', resize);
    return () => { viewport?.removeEventListener('resize', resize); window.removeEventListener('resize', resize); };
  }, []);
  const open = id => { location.hash = id ? `session=${encodeURIComponent(id)}` : ''; setSelected(id); };
  const toggleMute = preferences.data && !preferences.error ? async id => {
    if (muteLock.current) return;
    muteLock.current = true; setError('');
    try { await api('/api/push/preferences', { session_id:id, muted:!preferences.data.muted.includes(id) }); await preferences.reload(); }
    catch(e) { setError(e.message); } finally { muteLock.current = false; }
  } : null;
  return html`<div class=${`app ${selected && view === 'inbox' ? 'detail-open' : ''}`}><aside class="navigation"><a href="#" class="brand" aria-label="HQ home" onClick=${() => { setView('inbox'); open(null); }}><span class="brand-mark">hq<span/></span><span class="brand-caption">Your command center</span></a><nav aria-label="Main navigation">${[['inbox','Sessions'],['workers','Workers'],['routines','Routines'],['settings','Settings']].map(([id,label]) => html`<button class=${view === id ? 'active' : ''} aria-current=${view === id ? 'page' : undefined} onClick=${() => setView(id)}><${Icon} name=${id}/><span>${label}</span>${id === 'inbox' && sessions.data?.sessions.some(attention) && html`<i class="nav-dot"/>`}</button>`)}</nav><div class="nav-foot"><span class=${`connection-dot ${online ? '' : 'offline'}`}/>${online ? 'Workspace connected' : 'Device offline'}</div></aside>
    <main class="workspace">${!online && html`<div class="offline-banner" role="status">You’re offline. Reconnect to refresh sessions or send a message.</div>`}<${ErrorNotice} message=${error}/>${view === 'inbox' ? html`<div class="session-layout"><${Inbox} resource=${sessions} selected=${selected} open=${open}/>${selected ? html`<${Detail} key=${selected} id=${selected} snapshot=${sessions.data?.sessions.find(s => s.id === selected)} back=${() => open(null)} refreshList=${sessions.reload} muted=${preferences.data?.muted.includes(selected)} toggleMute=${toggleMute}/>` : html`<div class="detail welcome"><div class="welcome-art" aria-hidden="true"><${Icon} name="terminal" size=${38}/></div><h2>A place for every session.</h2><p>Pick up a conversation, answer a question,<br/>or see how the work is coming along.</p><span>Select a session to get started</span></div>`}</div>` : view === 'settings' ? html`<${Settings} auth=${auth} logout=${logout}/>` : html`<${Operations} key=${view} view=${view}/>`}</main></div>`;
}
function App() {
  const auth = useResource('/api/auth');
  const [error, setError] = useState('');
  useEffect(() => { const expired = () => auth.reload(); window.addEventListener('hq-auth-required', expired); return () => window.removeEventListener('hq-auth-required', expired); }, []);
  useEffect(() => { if (window.isSecureContext && 'serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js', { scope:'/' }).catch(e => console.warn('HQ service worker:', e.message)); }, []);
  if (!auth.hasLoaded) return html`<div class="boot"><${Loading} label="Opening HQ"/></div>`;
  if (!auth.data || auth.error) return html`<div class="boot"><${ErrorNotice} message=${auth.error || 'Cannot connect to HQ.'} retry=${auth.reload}/></div>`;
  if (auth.data.required && !auth.data.authenticated) return html`<${Pairing} paired=${auth.reload}/>`;
  return html`<${ErrorNotice} message=${error}/><${Workspace} auth=${auth.data} logout=${async () => { try { await api('/api/auth/logout', {}); await auth.reload(); } catch(e) { setError(e.message); } }}/>`;
}
render(html`<${App}/>`, document.getElementById('root'));
