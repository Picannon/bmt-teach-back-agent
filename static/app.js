// Part 1: just prove the phone can reach the laptop API end-to-end.
// Later parts render the questions and add the check-in flow + notifications.

const checkBtn = document.getElementById("check");
const statusEl = document.getElementById("status");

function setStatus(message, kind) {
  statusEl.textContent = message;
  statusEl.className = "status" + (kind ? ` status--${kind}` : "");
}

checkBtn.addEventListener("click", async () => {
  setStatus("Contacting the server…");
  checkBtn.disabled = true;
  try {
    const res = await fetch("/api/questions");
    const data = await res.json();
    if (!res.ok) {
      setStatus(`Server error: ${data.error || res.status}`, "err");
    } else {
      const count = data.questions?.length ?? 0;
      setStatus(`Connected — loaded ${count} question(s).`, "ok");
    }
  } catch (err) {
    setStatus(`Could not reach the server: ${err.message}`, "err");
  } finally {
    checkBtn.disabled = false;
  }
});