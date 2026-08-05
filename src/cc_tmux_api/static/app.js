// cc-tmux-api frontend — workers + routines over the 4-endpoint API.
// Preact + htm from esm.sh, no build step: edit, refresh.
import { h, render } from "https://esm.sh/preact@10.19.3";
import { useState, useEffect, useRef } from "https://esm.sh/preact@10.19.3/hooks";
import htm from "https://esm.sh/htm@3.1.1";

const html = htm.bind(h);

async function api(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

function useToast() {
  const [msg, setMsg] = useState("");
  const toast = (m, ms = 3500) => {
    setMsg(m);
    setTimeout(() => setMsg(""), ms);
  };
  return [msg, toast];
}

// destructive buttons arm on first click, fire on second
function ArmedButton({ label, className, onFire }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(t);
  }, [armed]);
  return html`<button class=${armed ? "confirm" : className}
    onClick=${() => (armed ? (setArmed(false), onFire()) : setArmed(true))}>
    ${armed ? "sure? " + label : label}</button>`;
}

const STATUS_LABEL = {
  working: "working", waiting: "needs input ⚠", idle: "finished turn",
  done: "claude exited", unknown: "…",
};

function WorkerCard({ w, toast, refresh }) {
  const [text, setText] = useState("");
  const termRef = useRef();
  useEffect(() => { // keep terminal scrolled to bottom as new output streams in
    if (termRef.current) termRef.current.scrollTop = termRef.current.scrollHeight;
  }, [w.tail]);

  const refine = async () => {
    if (!text.trim()) return;
    await api("/api/workers/action", { action: "send", session: w.session, text });
    toast("sent to " + w.session);
    setText("");
    setTimeout(refresh, 1200);
  };

  return html`<div class="card" id=${"w-" + w.session}>
    <div class="meta">
      <span class=${"wdot " + w.status}></span>
      <b>${w.title}</b>
      <span class="badge kind">${STATUS_LABEL[w.status] || w.status}</span>
      <span class="badge age">${Math.floor(w.age_s / 60)}m</span>
      ${w.attached && html`<span class="badge">👀 attached</span>`}
      ${w.web_url && html`<a href=${w.web_url} target="_blank">claude.ai ↗</a>`}
      <span style="margin-inline-start:auto" class="sub">tmux attach -t ${w.session}</span>
    </div>
    <pre class="term" ref=${termRef}>${w.tail}</pre>
    <div class="actions">
      <input type="text" value=${text}
        placeholder="refine / steer — sends into the live session…"
        onInput=${(e) => setText(e.target.value)}
        onKeyDown=${(e) => e.key === "Enter" && refine()} />
      <button onClick=${refine}>↳ Send</button>
      <${ArmedButton} label="✕ Kill" className="danger" onFire=${async () => {
        await api("/api/workers/action", { action: "kill", session: w.session });
        toast("killed " + w.session);
        refresh();
      }} />
    </div>
  </div>`;
}

function App() {
  const [data, setData] = useState(null);
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [toastMsg, toast] = useToast();

  const load = async () => {
    if (document.activeElement?.tagName === "TEXTAREA") return;
    try { setData(await (await fetch("/api/workers")).json()); } catch { /* keep old */ }
  };
  useEffect(() => {
    load();
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, []);

  // Deep-link to a specific session: /#w-<session>. The list renders async,
  // so scroll once the card appears; re-arm on hashchange.
  const focusedHash = useRef("");
  useEffect(() => {
    const id = decodeURIComponent(location.hash.slice(1));
    if (!id || focusedHash.current === id) return;
    const el = document.getElementById(id);
    if (!el) return; // not rendered yet — retry when data updates
    focusedHash.current = id;
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 2400);
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [data]);
  useEffect(() => {
    const onHash = () => { focusedHash.current = ""; load(); };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const spawn = async () => {
    if (!title.trim() || !prompt.trim()) { toast("title + prompt required"); return; }
    const r = await api("/api/workers/action", { action: "spawn", title, prompt });
    if (r.ok) { toast("spawned " + r.session); setTitle(""); setPrompt(""); }
    else toast("⚠ " + (r.output || "spawn failed"));
    setTimeout(load, 1500);
  };

  const ws = data?.workers || [];
  const others = data?.others || [];
  const routines = data?.routines || [];
  const fmtTs = (t) => (t ? new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "—");

  return html`
    <header><h1>cc-tmux-api</h1>
      <span class="sub">unattended Claude Code workers in tmux</span>
      <div class="spacer"></div>
      <span class="sub">${ws.length} workers</span>
    </header>
    <main>
      <h2 class="section-head">🤖 Workers <span class="count">${ws.length}</span></h2>
      ${ws.map((w) => html`<${WorkerCard} key=${w.session} w=${w} toast=${toast} refresh=${load} />`)}
      ${!ws.length && html`<div class="empty">no workers running</div>`}
      <details class="card" style="padding:10px 16px">
        <summary style="cursor:pointer">＋ launch a worker</summary>
        <div class="launch-form" style="margin-top:10px">
          <input type="text" placeholder="title" value=${title} onInput=${(e) => setTitle(e.target.value)} />
          <textarea placeholder="prompt — any task…" value=${prompt} onInput=${(e) => setPrompt(e.target.value)}></textarea>
          <div><button class="primary" onClick=${spawn}>🚀 Spawn</button>
            <span class="sub" style="margin-inline-start:8px">runs claude --dangerously-skip-permissions in tmux</span></div>
        </div>
      </details>

      ${routines.length > 0 && html`
        <h2 class="section-head">🔁 Routines <span class="count">${routines.length}</span></h2>
        <div class="rows">
          ${routines.map((r) => html`<div class="row" key=${r.name} style="align-items:center">
            <span class="rmain">
              <span class="rtext">${r.title} <span class="sub">· ${r.every}${r.days.length ? " " + r.days.join(",") : ""}</span></span>
              <span class="rbucket">last: ${fmtTs(r.last_run)} · next: ${r.enabled ? fmtTs(r.next_run) : "off"}${r.running ? " · running now: " + r.running : ""}</span>
            </span>
            <button onClick=${async () => {
              const res = await api("/api/routines/action", { action: "run", name: r.name });
              toast(res.ok ? "spawned " + res.session : "⚠ " + res.output);
              setTimeout(load, 1500);
            }}>▶ run now</button>
            <button onClick=${async () => {
              await api("/api/routines/action", { action: "toggle", name: r.name, enabled: !r.enabled });
              toast((r.enabled ? "disabled " : "enabled ") + r.name);
              load();
            }}>${r.enabled ? "on ✓" : "off"}</button>
          </div>`)}
        </div>`}

      ${others.length > 0 && html`<details style="margin-top:16px">
        <summary style="cursor:pointer;color:var(--dim)">🖥 ${others.length} other tmux sessions (yours, read-only)</summary>
        <div class="rows" style="margin-top:6px">
          ${others.map((o) => html`<div class="row" key=${o.session}>
            <span class="rmain"><span class="rtext">${o.session}</span>
            <span class="rbucket">${o.command}${o.attached ? " · attached" : ""}</span></span>
          </div>`)}
        </div>
      </details>`}
    </main>
    ${toastMsg && html`<div class="toast">${toastMsg}</div>`}`;
}

render(html`<${App} />`, document.getElementById("root"));
