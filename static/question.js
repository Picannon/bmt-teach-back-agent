// Conversation page: the notified question is turn 1. On submit, the server
// drives teach_back_agent and returns this turn's events (note / escalate /
// next question / finish), which we append to the thread. Adaptive follow-ups
// keep the conversation going until the agent finishes.

const params = new URLSearchParams(window.location.search);
const firstQuestionId = params.get("id");
const thread = document.getElementById("thread");

let conversationId = null; // null until the first submit returns one

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function scrollDown() {
  window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
}

function addQuestion(text) {
  // Only the question — never the topic label, which can give away the answer.
  const card = el("section", "card");
  card.appendChild(el("h2", "card__title", text));
  thread.appendChild(card);
}

// Render an answer box + Submit for the active question, and wire submission.
function addAnswerInput() {
  const ta = el("textarea", "answer");
  ta.rows = 3;
  ta.placeholder = "Type your answer in your own words…";
  const btn = el("button", "btn", "Submit");
  thread.appendChild(ta);
  thread.appendChild(btn);
  btn.addEventListener("click", () => submitAnswer(ta, btn));
  ta.focus();
  scrollDown();
}

function addEvent(ev) {
  if (ev.type === "note") {
    thread.appendChild(
      el("div", "line line--note", `📝 Note recorded in your clinical document: ${ev.text}`)
    );
  } else if (ev.type === "escalate") {
    thread.appendChild(
      el("div", "line line--escalate", `⚠️ Escalating to your clinician: ${ev.text}`)
    );
  } else if (ev.type === "finish") {
    thread.appendChild(el("div", "line line--finish", ev.text));
  } else if (ev.type === "question") {
    addQuestion(ev.text);
    addAnswerInput();
  }
  scrollDown();
}

async function submitAnswer(ta, btn) {
  const answer = ta.value.trim();
  if (!answer) return;

  // Lock this turn's input.
  ta.readOnly = true;
  ta.classList.add("answer--locked");
  btn.remove();

  const body = conversationId
    ? { conversation_id: conversationId, answer }
    : { question_id: Number(firstQuestionId), answer };

  const thinking = el("div", "line line--thinking", "Thinking…");
  thread.appendChild(thinking);
  scrollDown();

  try {
    const res = await fetch("/api/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    thinking.remove();
    if (!res.ok) {
      thread.appendChild(
        el("div", "line line--escalate", data.detail || data.error || "Something went wrong.")
      );
      return;
    }
    conversationId = data.conversation_id;
    for (const ev of data.events) addEvent(ev);
  } catch (err) {
    thinking.remove();
    thread.appendChild(el("div", "line line--escalate", err.message));
  }
}

async function loadFirstQuestion() {
  if (!firstQuestionId) {
    addQuestion("No question specified.");
    return;
  }
  try {
    const res = await fetch(`/api/questions/${firstQuestionId}`);
    const q = await res.json();
    if (!res.ok) {
      addQuestion("Question not found.");
      return;
    }
    addQuestion(q.question);
    addAnswerInput();
  } catch (_) {
    addQuestion("Could not load the question.");
  }
}

loadFirstQuestion();
