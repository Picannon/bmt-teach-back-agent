# BMT Part 1 — App Infrastructure

**Goal:** Get a mobile-styled app serving from the laptop and viewable on the
iPhone, so we have a real visual for what the demo will look like. Just the
plumbing + a skeleton screen — the questions UI and notifications come in later
parts.

## High-level overview

We turn `generate_questions.py` into a small web app that **looks like a mobile
app**, run it on the laptop, and open it from the iPhone for the demo video.

- **The app runs on the laptop.** A FastAPI server serves the questions as JSON
  (`/api/questions`, wrapping our existing `generate_questions()`) and serves a
  mobile-styled web page (plain HTML/CSS/JS — no build step) that fetches them.
- **The phone just views it.** Nothing is downloaded or installed on the iPhone.
  We expose the laptop server with a **Cloudflare Tunnel** (`cloudflared`, free,
  no signup) — one command prints a public `https://…trycloudflare.com` URL that
  forwards to the laptop. Open that URL in Safari and the app is running on the
  phone. Optionally *Add to Home Screen* for a fullscreen, app-like icon.
- **Why Cloudflare Tunnel:** it gives us HTTPS for free, which the browser
  requires for notifications and home-screen install (plain `http://LAN-ip`
  won't do). `ngrok` also works but now needs an account; `cloudflared` doesn't.
- **Demo = a short screen recording** on the phone. Since we can re-record
  freely, we skip performance work and call Claude live.

## Part 1 scope — just the skeleton

1. **Add deps:** `fastapi`, `uvicorn` → `requirements.txt`, install into `.venv`.
2. **`server.py`:** FastAPI app that
   - serves `static/index.html` at `/`
   - exposes `GET /api/questions` (calls `generate_questions()` for now)
3. **`static/index.html` + `styles.css`:** a minimal phone-width screen with the
   app name and a placeholder — enough to confirm it *looks* like a mobile app.
   No real question rendering yet.
4. **Run locally:** `uvicorn server:app --reload`, confirm the page loads and
   `/api/questions` returns JSON on the laptop.
5. **Expose it:** `brew install cloudflared`, then
   `cloudflared tunnel --url http://localhost:8000` → copy the HTTPS URL.
6. **View on iPhone:** open the URL in Safari; optionally *Add to Home Screen*.

**Done when:** the skeleton app loads on the iPhone through the tunnel and looks
like a mobile app.

## Deferred to later parts

- Rendering the real question cards + teach-back check-in flow
- Notifications (in-app banner + real-notification attempt)
- PWA manifest polish (custom home-screen icon / splash)