// Phase 4 — Voice Live client (Path A).
// Captures mic at 24 kHz mono PCM16, streams to bridge /voicelive/ws,
// plays back model audio chunks, renders live transcript and per-turn
// latency on the dashboard.

const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const transcriptEl = document.getElementById("transcript");
const metricsBody = document.getElementById("metricsBody");

const SAMPLE_RATE = 24000;
const FRAME_MS = 40;

let ws = null;
let audioCtx = null;
let micStream = null;
let workletNode = null;
let playbackTime = 0;
let turn = null;
const turns = []; // {firstAudioMs, fullMs}

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

function renderMetrics() {
  if (!turns.length) return;
  const last = turns[turns.length - 1];
  const firsts = turns.map((t) => t.firstAudioMs).filter(Boolean);
  const fulls = turns.map((t) => t.fullMs).filter(Boolean);
  metricsBody.innerHTML = `
    <tr><td>First audio</td><td>${last.firstAudioMs ?? "—"}</td><td>${pct(firsts, 50)}</td><td>${pct(firsts, 95)}</td></tr>
    <tr><td>Full response</td><td>${last.fullMs ?? "—"}</td><td>${pct(fulls, 50)}</td><td>${pct(fulls, 95)}</td></tr>
  `;
}

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

  if (turn && !turn.firstAudioMs) {
    turn.firstAudioMs = Math.round(performance.now() - turn.startedAt);
    renderMetrics();
  }
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
      turn = { startedAt: performance.now() };
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
    case "response.audio.done":
    case "response.done":
      if (turn) {
        turn.fullMs = Math.round(performance.now() - turn.startedAt);
        turns.push(turn);
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

function handleEventB(evt) {
  switch (evt.type) {
    case "transcript.partial":
      if (!turn && (evt.text || "").trim()) {
        turn = { startedAt: performance.now() };
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
      // server-measured first-audio (utterance start -> first synth byte sent)
      if (turn && evt.first_audio_ms != null && !turn.firstAudioMs) {
        turn.firstAudioMs = evt.first_audio_ms;
        renderMetrics();
      }
      break;
    case "response.done":
      if (turn) {
        turn.fullMs = Math.round(performance.now() - turn.startedAt);
        turns.push(turn);
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
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const route = path === "B" ? "/composed/ws" : "/voicelive/ws";
  ws = new WebSocket(`${proto}://${location.host}${route}`);
  const handler = path === "B" ? handleEventB : handleEventA;
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
