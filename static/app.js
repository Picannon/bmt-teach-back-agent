// Home screen: poll the server every 0.5s. The endpoint is consume-on-read, so
// each notification comes back exactly once — no client-side tracking needed.
// When one arrives, pop the banner (and a real OS notification if allowed).
// Tapping it opens the dedicated question page.

const banner = document.getElementById("banner");
const bannerTitle = document.getElementById("banner-title");
const bannerBody = document.getElementById("banner-body");

let currentQuestionId = null;

// Best-effort OS notification permission. If denied/unsupported we just use the
// in-app banner, so nothing breaks.
if ("Notification" in window && Notification.permission === "default") {
  Notification.requestPermission().catch(() => {});
}

function goToQuestion(id) {
  window.location.href = `/question.html?id=${id}`;
}

function showNotification(n) {
  currentQuestionId = n.question_id;
  bannerTitle.textContent = n.title;
  bannerBody.textContent = n.body;
  banner.hidden = false;
  // restart the slide-in animation
  banner.classList.remove("banner--in");
  void banner.offsetWidth;
  banner.classList.add("banner--in");

  // Bonus: real OS notification when permission was granted.
  if ("Notification" in window && Notification.permission === "granted") {
    try {
      const osNotif = new Notification(n.title, { body: n.body });
      osNotif.onclick = () => goToQuestion(n.question_id);
    } catch (_) {
      /* fall back to the banner */
    }
  }
}

banner.addEventListener("click", () => {
  if (currentQuestionId !== null) goToQuestion(currentQuestionId);
});

async function poll() {
  try {
    const res = await fetch("/api/notifications");
    if (!res.ok) return;
    const { notifications } = await res.json();
    for (const n of notifications) showNotification(n); // newest wins
  } catch (_) {
    // transient network hiccup — keep polling
  }
}

setInterval(poll, 500);
poll();