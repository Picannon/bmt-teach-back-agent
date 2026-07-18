// Question page: read the id from the URL, fetch the question, let the patient
// answer, POST it for evaluation, and render the agent's response.

const params = new URLSearchParams(window.location.search);
const id = params.get("id");

const topicEl = document.getElementById("topic");
const questionEl = document.getElementById("question");
const answerEl = document.getElementById("answer");
const submitBtn = document.getElementById("submit");
const agentEl = document.getElementById("agent");
const verdictEl = document.getElementById("verdict");
const feedbackEl = document.getElementById("feedback");

async function loadQuestion() {
  if (id === null) {
    topicEl.textContent = "";
    questionEl.textContent = "No question specified.";
    return;
  }
  try {
    const res = await fetch(`/api/questions/${id}`);
    if (!res.ok) {
      questionEl.textContent = "Question not found.";
      return;
    }
    const q = await res.json();
    topicEl.textContent = q.topic || "Teach-back";
    questionEl.textContent = q.question;
  } catch (_) {
    questionEl.textContent = "Could not load the question.";
  }
}

async function submitAnswer() {
  const answer = answerEl.value.trim();
  if (!answer) return;

  submitBtn.disabled = true;
  submitBtn.textContent = "Checking…";
  try {
    const res = await fetch("/api/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question_id: Number(id), answer }),
    });
    const data = await res.json();
    if (!res.ok) {
      verdictEl.textContent = "Error";
      feedbackEl.textContent = data.detail || data.error || "Something went wrong.";
    } else {
      const evaluation = data.evaluation || {};
      verdictEl.textContent = evaluation.verdict || "";
      feedbackEl.textContent = evaluation.feedback || JSON.stringify(evaluation);
    }
    agentEl.hidden = false;
  } catch (err) {
    verdictEl.textContent = "Error";
    feedbackEl.textContent = err.message;
    agentEl.hidden = false;
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "Submit";
  }
}

submitBtn.addEventListener("click", submitAnswer);
loadQuestion();