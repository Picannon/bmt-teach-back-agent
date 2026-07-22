// Voice agent page: stream the phone mic to the server over a WebSocket, show the
// agent's state (asleep → listening → capturing → done) and the live transcript.
// The server proxies audio to Deepgram and generates teach-back questions on stop.

const PATIENT_ID = "demo-patient";

const pill = document.getElementById("pill");
const hint = document.getElementById("hint");
const armBtn = document.getElementById("arm");
const listenBtn = document.getElementById("listen");
const stopBtn = document.getElementById("stop");
const synthBtn = document.getElementById("synthetic");
const liveCard = document.getElementById("live-card");
const transcriptEl = document.getElementById("transcript");
const interimEl = document.getElementById("interim");
const resultCard = document.getElementById("result-card");
const resultText = document.getElementById("result-text");
const goTeachback = document.getElementById("go-teachback");

let ws = null;
let recorder = null;
let stream = null;

const PILLS = {
  asleep: ["💤 asleep", "pill--asleep"],
  listening: ["👂 listening", "pill--listening"],
  capturing: ["🔴 capturing", "pill--capturing"],
  generating: ["✨ generating", "pill--capturing"],
  done: ["✅ done", "pill--done"],
};

function setPill(state) {
  const [label, cls] = PILLS[state] || PILLS.asleep;
  pill.textContent = label;
  pill.className = "pill " + cls;
}

async function refreshStatus() {
  try {
    const res = await fetch(`/api/voice/status?patient_id=${PATIENT_ID}`);
    const s = await res.json();
    listenBtn.disabled = !s.armed;
    if (s.armed) {
      hint.textContent = s.deepgram_configured
        ? "Armed. Tap Start listening — the agent captures once it hears the class."
        : "Armed, but DEEPGRAM_API_KEY isn't set on the server. Use the synthetic fallback.";
      armBtn.hidden = true;
    }
    return s;
  } catch (_) {
    return null;
  }
}

armBtn.addEventListener("click", async () => {
  armBtn.disabled = true;
  await fetch("/api/voice/arm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ patient_id: PATIENT_ID }),
  });
  await refreshStatus();
  armBtn.disabled = false;
});

listenBtn.addEventListener("click", startListening);
stopBtn.addEventListener("click", stopListening);

synthBtn.addEventListener("click", async () => {
  synthBtn.disabled = true;
  synthBtn.textContent = "Generating from synthetic transcript…";
  setPill("generating");
  try {
    const res = await fetch("/api/voice/synthetic", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patient_id: PATIENT_ID }),
    });
    const data = await res.json();
    showResult(data.question_ids, data.count);
    setPill("done");
  } catch (_) {
    hint.textContent = "Synthetic fallback failed — check the server logs.";
  } finally {
    synthBtn.disabled = false;
    synthBtn.textContent = "Use synthetic transcript (fallback)";
  }
});

async function startListening() {
  liveCard.hidden = false;
  resultCard.hidden = true;
  transcriptEl.textContent = "";
  interimEl.textContent = "";

  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (_) {
    hint.textContent = "Microphone permission denied — needed to listen.";
    return;
  }

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/api/voice/stream?patient_id=${PATIENT_ID}`);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    recorder = new MediaRecorder(stream, pickMime());
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0 && ws && ws.readyState === WebSocket.OPEN) ws.send(e.data);
    };
    recorder.start(250); // emit a chunk 4x/sec for low-latency streaming
    listenBtn.hidden = true;
    stopBtn.hidden = false;
  };

  ws.onmessage = (evt) => handleEvent(JSON.parse(evt.data));
  ws.onclose = () => cleanupAudio();
  ws.onerror = () => { hint.textContent = "Connection error."; };
}

function handleEvent(msg) {
  if (msg.state) setPill(msg.state);
  if (msg.type === "capture") {
    transcriptEl.textContent = msg.transcript || transcriptEl.textContent;
    interimEl.textContent = "";
  } else if (msg.type === "interim") {
    interimEl.textContent = msg.text || "";
  } else if (msg.type === "generating") {
    interimEl.textContent = "";
    hint.textContent = "Heard the class — generating teach-back questions…";
  } else if (msg.type === "done") {
    if (msg.captured) showResult(msg.question_ids, msg.count);
    else hint.textContent = msg.message || "Nothing on-topic was captured.";
  } else if (msg.type === "error") {
    hint.textContent = msg.message || "Something went wrong.";
  }
}

function showResult(questionIds, count) {
  resultCard.hidden = false;
  resultText.textContent = `Generated ${count} teach-back question${count === 1 ? "" : "s"} from what was said in class.`;
  const first = (questionIds || [])[0];
  goTeachback.href = first != null ? `/question.html?id=${first}` : "/";
}

function stopListening() {
  stopBtn.disabled = true;
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stop" }));
  if (recorder && recorder.state !== "inactive") recorder.stop();
}

function cleanupAudio() {
  if (recorder && recorder.state !== "inactive") recorder.stop();
  if (stream) stream.getTracks().forEach((t) => t.stop());
  recorder = null;
  stream = null;
  stopBtn.hidden = true;
  stopBtn.disabled = false;
  listenBtn.hidden = false;
}

function pickMime() {
  for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return { mimeType: m };
  }
  return {};
}

setPill("asleep");
refreshStatus();
