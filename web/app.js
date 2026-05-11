// Phase 4 — Voice Live client (Path A).
// Captures mic at 24 kHz mono PCM16, streams to bridge /voicelive/ws,
// plays back model audio chunks, renders live transcript and per-turn
// latency on the dashboard.

const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const resetBtn = document.getElementById("resetBtn");
const transcriptEl = document.getElementById("transcript");
const statsBodies = {
  A: document.getElementById("statsA"),
  B: document.getElementById("statsB"),
  C: document.getElementById("statsC"),
};
const countEls = {
  A: document.getElementById("countA"),
  B: document.getElementById("countB"),
  C: document.getElementById("countC"),
};
const breakdownEl = document.getElementById("breakdownB");
const breakdownBar = document.getElementById("breakdownBar");
const breakdownLegend = document.getElementById("breakdownLegend");

// max ms used to scale the horizontal bars (4 s = "full bar")
const BAR_SCALE_MS = 4000;
// Path A perceived-responsiveness target (green marker on the bar)
const TARGET_A_MS = 1500;

const SAMPLE_RATE = 24000;
const FRAME_MS = 40;

let ws = null;
let audioCtx = null;
let micStream = null;
let workletNode = null;
let playbackTime = 0;
let scheduledNodes = [];
let turn = null;
let activePath = "A"; // path the current call/turn belongs to
const turnsByPath = { A: [], B: [], C: [] }; // {firstAudioMs, finalToFirstAudioMs, fullMs}

function setRunning(running) {
  startBtn.disabled = running;
  stopBtn.disabled = !running;
}

function logTranscript(role, text, append = false) {
  if (!text) return;
  let line = transcriptEl.querySelector(`.line.${role}.live`);
  if (!line || !append) {
    line = document.createElement("div");
    line.className = `line ${role} live`;
    transcriptEl.appendChild(line);
  }
  if (append) line.textContent += text;
  else line.textContent = `${role === "user" ? "You" : "Bot"}: ${text}`;
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function finalizeLine() {
  transcriptEl
    .querySelectorAll(".line.live")
    .forEach((l) => l.classList.remove("live"));
}

function pct(arr, p) {
  if (!arr.length) return "—";
  const s = [...arr].sort((a, b) => a - b);
  const i = Math.min(s.length - 1, Math.floor((p / 100) * s.length));
  return Math.round(s[i]);
}

function fmt(v) {
  return v == null ? "—" : Math.round(v).toLocaleString();
}

function fmtSec(ms) {
  if (ms == null) return "—";
  return (ms / 1000).toFixed(2) + "s";
}

function barWidth(ms) {
  if (ms == null) return 0;
  return Math.min(100, Math.max(2, (ms / BAR_SCALE_MS) * 100));
}

function statHTML(name, value, p50, p95, opts = {}) {
  const path = opts.path || "A";
  const target = opts.target;
  const cls = value == null
    ? ""
    : (target && value <= target ? "good" : (value > target * 1.5 ? "warn" : ""));
  const targetMarker = target
    ? `<span class="target" style="left:${barWidth(target)}%"></span>`
    : "";
  return `
    <div class="stat">
      <div class="stat-row">
        <span class="stat-name">${name}</span>
        <span class="stat-value ${cls}">${fmt(value)}<span class="unit">ms</span></span>
      </div>
      <div class="stat-bar ${path === "B" ? "b" : path === "C" ? "c" : ""}">
        <span style="width:${barWidth(value)}%"></span>
        ${targetMarker}
      </div>
      <div class="stat-meta">
        <span>p50 <b>${fmt(p50)}</b></span>
        <span>p95 <b>${fmt(p95)}</b></span>
        ${target ? `<span>target <b>&lt; ${fmt(target)}</b></span>` : ""}
      </div>
    </div>
  `;
}

function renderBreakdownC(last) {
  const el = document.getElementById("breakdownC");
  const bar = document.getElementById("breakdownBarC");
  const legend = document.getElementById("breakdownLegendC");
  if (!el || !bar || !legend) return;
  if (!last || !last.breakdown) { el.hidden = true; return; }
  const b = last.breakdown;
  const measured =
    (b.embed_ms || 0) + (b.search_ms || 0) + (b.llm_ttft_ms || 0) + (b.tts_first_byte_ms || 0);
  const baseFirstAudio = last.finalToFirstAudioMs ?? last.firstAudioMs;
  const stt = Math.max(0, (baseFirstAudio || 0) - measured);
  const segs = [
    { key: "stt",    label: "STT",      ms: stt,                cls: "seg-stt"    },
    { key: "embed",  label: "Embed",    ms: b.embed_ms  || 0,  cls: "seg-embed"  },
    { key: "search", label: "Search",   ms: b.search_ms || 0,  cls: "seg-search" },
    { key: "llm",    label: "LLM TTFT", ms: b.llm_ttft_ms || 0, cls: "seg-llm"  },
    { key: "tts",    label: "TTS",      ms: b.tts_first_byte_ms || 0, cls: "seg-tts" },
  ];
  const total = segs.reduce((s, x) => s + x.ms, 0) || 1;
  bar.innerHTML = segs.map((s) => {
    const p = (s.ms / total) * 100;
    const labelInside = p >= 12 ? `${s.label} ${Math.round(s.ms)}ms` : "";
    return `<div class="seg ${s.cls}" style="flex-basis:${p}%" title="${s.label}: ${Math.round(s.ms)} ms">${labelInside}</div>`;
  }).join("");
  legend.innerHTML = segs.map((s) =>
    `<span class="lg"><span class="swatch ${s.cls}"></span>${s.label} <b>${Math.round(s.ms)}ms</b></span>`
  ).join("");
  el.hidden = false;
}

function renderBreakdown(last) {
  if (!last || !last.breakdown) {
    breakdownEl.hidden = true;
    return;
  }
  const b = last.breakdown;
  // Sequential breakdown for Path B first-audio:
  //   STT end-of-utterance silence (Speech SDK ~500-800ms, not measured) +
  //   embed + search + LLM TTFT + TTS first byte.
  // We don't measure STT silence directly; we infer it as
  // final_to_first_audio - (embed + search + llm_ttft + tts_first_byte).
  // If final_to_first is absent, fall back to speech_start_to_first.
  const measured =
    (b.embed_ms || 0) + (b.search_ms || 0) + (b.llm_ttft_ms || 0) + (b.tts_first_byte_ms || 0);
  const baseFirstAudio = last.finalToFirstAudioMs ?? last.firstAudioMs;
  const stt = Math.max(0, (baseFirstAudio || 0) - measured);
  const segs = [
    { key: "stt", label: "STT", ms: stt, cls: "seg-stt" },
    { key: "embed", label: "Embed", ms: b.embed_ms || 0, cls: "seg-embed" },
    { key: "search", label: "Search", ms: b.search_ms || 0, cls: "seg-search" },
    { key: "llm", label: "LLM TTFT", ms: b.llm_ttft_ms || 0, cls: "seg-llm" },
    { key: "tts", label: "TTS", ms: b.tts_first_byte_ms || 0, cls: "seg-tts" },
  ];
  const total = segs.reduce((s, x) => s + x.ms, 0) || 1;
  breakdownBar.innerHTML = segs
    .map((s) => {
      const pct = (s.ms / total) * 100;
      const labelInside = pct >= 12 ? `${s.label} ${Math.round(s.ms)}ms` : "";
      return `<div class="seg ${s.cls}" style="flex-basis:${pct}%" title="${s.label}: ${Math.round(s.ms)} ms">${labelInside}</div>`;
    })
    .join("");
  breakdownLegend.innerHTML = segs
    .map((s) => `
      <span class="lg"><span class="swatch ${s.cls}"></span>${s.label} <b>${Math.round(s.ms)}ms</b></span>
    `)
    .join("");
  breakdownEl.hidden = false;
}

function renderMetricsFor(path) {
  const tbody = statsBodies[path];
  if (!tbody) return;
  const turns = turnsByPath[path];
  countEls[path].textContent = `${turns.length} turn${turns.length === 1 ? "" : "s"}`;
  if (!turns.length) {
    const labels = { A: "start a call on Path A", B: "switch to Path B and start a call", C: "switch to Path C and start a call" };
    tbody.innerHTML = `<div class="empty-row">No turns yet — ${labels[path] || "start a call"}.</div>`;
    if (path === "B") breakdownEl.hidden = true;
    if (path === "C") { const bd = document.getElementById("breakdownC"); if (bd) bd.hidden = true; }
    return;
  }
  const last = turns[turns.length - 1];
  const firsts = turns.map((t) => t.firstAudioMs).filter((v) => v != null);
  const finals = turns.map((t) => t.finalToFirstAudioMs).filter((v) => v != null);
  const fulls = turns.map((t) => t.fullMs).filter((v) => v != null);
  const target = path === "A" ? TARGET_A_MS : null;
  tbody.innerHTML =
    statHTML("Speech→First audio", last.firstAudioMs, pct(firsts, 50), pct(firsts, 95), { path, target }) +
    statHTML("Final→First audio", last.finalToFirstAudioMs, pct(finals, 50), pct(finals, 95), { path }) +
    statHTML("Spoken response", last.fullMs, pct(fulls, 50), pct(fulls, 95), { path });
  if (path === "B") renderBreakdown(last);
  if (path === "C") renderBreakdownC(last);
}

function renderMetrics() {
  renderMetricsFor("A");
  renderMetricsFor("B");
  renderMetricsFor("C");
}

function resetMetrics() {
  turnsByPath.A.length = 0;
  turnsByPath.B.length = 0;
  turnsByPath.C.length = 0;
  renderMetrics();
}
renderMetrics();

// ---- audio capture --------------------------------------------------------

const WORKLET_SRC = `
class PCM16Sender extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf = [];
    this._frame = Math.round(${SAMPLE_RATE} * ${FRAME_MS} / 1000);
  }
  process(inputs) {
    const ch = inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) this._buf.push(ch[i]);
    while (this._buf.length >= this._frame) {
      const slice = this._buf.splice(0, this._frame);
      const out = new Int16Array(slice.length);
      for (let i = 0; i < slice.length; i++) {
        const s = Math.max(-1, Math.min(1, slice[i]));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(out.buffer, [out.buffer]);
    }
    return true;
  }
}
registerProcessor('pcm16-sender', PCM16Sender);
`;

async function startMic() {
  audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
  await audioCtx.resume();
  const blob = new Blob([WORKLET_SRC], { type: "application/javascript" });
  await audioCtx.audioWorklet.addModule(URL.createObjectURL(blob));
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      sampleRate: SAMPLE_RATE,
      echoCancellation: true,
      noiseSuppression: true,
    },
  });
  const src = audioCtx.createMediaStreamSource(micStream);
  workletNode = new AudioWorkletNode(audioCtx, "pcm16-sender");
  workletNode.port.onmessage = (e) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      // Voice Live event with base64 audio
      const b64 = btoa(
        String.fromCharCode(...new Uint8Array(e.data)),
      );
      ws.send(
        JSON.stringify({ type: "input_audio_buffer.append", audio: b64 }),
      );
    }
  };
  src.connect(workletNode);
  // workletNode.connect(audioCtx.destination); // do NOT echo mic
}

function stopMic() {
  try { workletNode?.disconnect(); } catch {}
  try { micStream?.getTracks().forEach((t) => t.stop()); } catch {}
  try { audioCtx?.close(); } catch {}
  workletNode = null;
  micStream = null;
  audioCtx = null;
  playbackTime = 0;
  scheduledNodes = [];
}

// ---- audio playback (Int16 PCM @ 24 kHz) ----------------------------------

function playPCM16(b64) {
  if (!audioCtx) return;
  const bin = atob(b64);
  const i16 = new Int16Array(bin.length / 2);
  for (let i = 0; i < i16.length; i++) {
    const lo = bin.charCodeAt(i * 2);
    const hi = bin.charCodeAt(i * 2 + 1);
    let v = (hi << 8) | lo;
    if (v & 0x8000) v -= 0x10000;
    i16[i] = v;
  }
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 0x8000;
  const buf = audioCtx.createBuffer(1, f32.length, SAMPLE_RATE);
  buf.copyToChannel(f32, 0, 0);
  const node = audioCtx.createBufferSource();
  node.buffer = buf;
  node.connect(audioCtx.destination);
  const now = audioCtx.currentTime;
  if (playbackTime < now) playbackTime = now;
  node.start(playbackTime);
  playbackTime += buf.duration;
  scheduledNodes.push(node);
  node.onended = () => {
    const i = scheduledNodes.indexOf(node);
    if (i >= 0) scheduledNodes.splice(i, 1);
  };

  if (turn && !turn.firstAudioMs) {
    turn.firstAudioMs = Math.round(performance.now() - turn.startedAt);
    renderMetrics();
  }
}

function clearPlayback() {
  for (const n of scheduledNodes) {
    try { n.onended = null; n.stop(); } catch {}
    try { n.disconnect(); } catch {}
  }
  scheduledNodes = [];
  if (audioCtx) playbackTime = audioCtx.currentTime;
}

// ---- WS handling ----------------------------------------------------------

function selectedPath() {
  const r = document.querySelector('input[name="path"]:checked');
  return r ? r.value : "A";
}

function handleEventA(evt) {
  switch (evt.type) {
    case "session.created":
    case "session.updated":
      logTranscript("system", "[connected]");
      break;
    case "input_audio_buffer.speech_started":
      turn = { startedAt: performance.now(), path: activePath };
      finalizeLine();
      break;
    case "conversation.item.input_audio_transcription.completed":
      logTranscript("user", evt.transcript || "");
      finalizeLine();
      break;
    case "response.audio_transcript.delta":
      logTranscript("bot", evt.delta || "", true);
      break;
    case "response.audio.delta":
      playPCM16(evt.delta);
      break;
    case "metrics":
      if (turn && evt.first_audio_ms != null && !turn.firstAudioMs) {
        turn.firstAudioMs = evt.first_audio_ms;
        if (evt.final_to_first_audio_ms != null) {
          turn.finalToFirstAudioMs = evt.final_to_first_audio_ms;
        }
        renderMetrics();
      }
      break;
    case "response.audio.done":
    case "response.done":
      if (turn) {
        turn.fullMs = Math.round(performance.now() - turn.startedAt);
        turnsByPath[turn.path || "A"].push(turn);
        turn = null;
        renderMetrics();
        finalizeLine();
      }
      break;
    case "error":
    case "bridge.error":
      logTranscript("system", `[error] ${evt.error?.message || evt.error}`);
      break;
  }
}

function handleEventC(evt) {
  switch (evt.type) {
    case "playback.clear":
      clearPlayback();
      turn = null;
      finalizeLine();
      break;
    case "transcript.partial":
      if (!turn && (evt.text || "").trim()) {
        turn = { startedAt: performance.now(), path: activePath };
      }
      logTranscript("user", evt.text || "");
      break;
    case "transcript.final":
      logTranscript("user", evt.text || "");
      finalizeLine();
      break;
    case "audio.delta":
      if (evt.text) logTranscript("bot", evt.text + " ", true);
      playPCM16(evt.audio);
      break;
    case "metrics":
      if (turn && evt.first_audio_ms != null && !turn.firstAudioMs) {
        turn.firstAudioMs = evt.first_audio_ms;
        if (evt.final_to_first_audio_ms != null) {
          turn.finalToFirstAudioMs = evt.final_to_first_audio_ms;
        }
        if (evt.breakdown) turn.breakdown = evt.breakdown;
        renderMetrics();
      }
      break;
    case "response.done":
      if (turn) {
        turn.fullMs = Math.round(performance.now() - turn.startedAt);
        turnsByPath[turn.path || "C"].push(turn);
        turn = null;
        renderMetrics();
        finalizeLine();
      }
      break;
    case "error":
      logTranscript("system", `[error] ${evt.error}`);
      break;
  }
}

function handleEventB(evt) {
  switch (evt.type) {
    case "playback.clear":
      clearPlayback();
      // forget any in-flight turn; a new one will be started on next final
      turn = null;
      finalizeLine();
      break;
    case "transcript.partial":
      if (!turn && (evt.text || "").trim()) {
        turn = { startedAt: performance.now(), path: activePath };
      }
      logTranscript("user", evt.text || "");
      break;
    case "transcript.final":
      logTranscript("user", evt.text || "");
      finalizeLine();
      break;
    case "audio.delta":
      if (evt.text) logTranscript("bot", evt.text + " ", true);
      playPCM16(evt.audio);
      break;
    case "metrics":
      // server-measured first-audio metrics
      if (turn && evt.first_audio_ms != null && !turn.firstAudioMs) {
        turn.firstAudioMs = evt.first_audio_ms;
        if (evt.final_to_first_audio_ms != null) {
          turn.finalToFirstAudioMs = evt.final_to_first_audio_ms;
        }
        if (evt.breakdown) turn.breakdown = evt.breakdown;
        renderMetrics();
      }
      break;
    case "response.done":
      if (turn) {
        turn.fullMs = Math.round(performance.now() - turn.startedAt);
        turnsByPath[turn.path || "B"].push(turn);
        turn = null;
        renderMetrics();
        finalizeLine();
      }
      break;
    case "error":
      logTranscript("system", `[error] ${evt.error}`);
      break;
  }
}

async function start() {
  setRunning(true);
  await startMic();
  const path = selectedPath();
  activePath = path;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const route = path === "B" ? "/composed/ws" : path === "C" ? "/pathc/ws" : "/voicelive/ws";
  ws = new WebSocket(`${proto}://${location.host}${route}`);
  const handler = path === "B" ? handleEventB : path === "C" ? handleEventC : handleEventA;
  ws.onopen = () => logTranscript("system", `[ws open path ${path}]`);
  ws.onerror = () => logTranscript("system", "[ws error]");
  ws.onclose = () => {
    logTranscript("system", "[ws closed]");
    setRunning(false);
    stopMic();
  };
  ws.onmessage = (e) => {
    if (typeof e.data !== "string") return;
    try { handler(JSON.parse(e.data)); } catch {}
  };
}

function stop() {
  try { ws?.close(); } catch {}
  ws = null;
  setRunning(false);
  stopMic();
}

startBtn.addEventListener("click", start);
stopBtn.addEventListener("click", stop);
resetBtn?.addEventListener("click", resetMetrics);
