# JARVIS (v0.1 – Core MVP)

A voice assistant for your Windows laptop. Say "Jarvis, open Chrome" and it does it.

## What works now
- Wake word ("Jarvis") + follow-up conversation without repeating it
- Free AI brain (Groq or Gemini free tier, or local Ollama) that chains tools
- PC control: open apps/sites/files, find files, type, hotkeys, volume, media, screenshot, clipboard, PowerShell, lock/sleep/shutdown
- Web: search, scrape any page (optionally by CSS selector)
- Food / shopping: opens Swiggy, Zomato, Amazon, Flipkart searches (you finish checkout)
- PA: tasks, deadlines, voice reminders, daily briefing
- Starts automatically at login, unlock and wake-from-sleep
- Phone API (tasks, status, commands) ready for the Android app

## Setup (Windows)
1. Install Python 3.10+ from python.org (tick "Add to PATH").
2. In this folder:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
   If `PyAudio` fails: `pip install pipwin` then `pipwin install pyaudio`.
3. `copy .env.example .env`, then paste a free key:
   - Groq: https://console.groq.com/keys (recommended, fast)
   - Gemini: https://aistudio.google.com/apikey (set `JARVIS_PROVIDER=gemini`)
4. Test without a mic: `python run.py --text`
5. Voice mode: `python run.py`
6. Auto-start: `powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1`

## Phone
`python run.py --pair` prints the address and token. Same Wi-Fi only for now;
for use anywhere, install Tailscale on both devices (don't open router ports).
Endpoints (header `Authorization: Bearer <token>`): `GET /tasks`, `GET /status`,
`POST /tasks`, `POST /tasks/<id>/complete`, `POST /command {"text": "..."}`.
Windows Firewall will ask to allow Python on first run – allow Private networks.

## Safety
Risky actions (delete, install, restart, shutdown, redirects) are spoken back and need a spoken "yes".
Commands from the phone are NOT auto-approved unless the request sets `auto_confirm`.
Keep your token private: anyone with it can control your PC.

## Roadmap
1. Native Android app (Kotlin) using the API above, plus push notifications
2. Offline wake word (Porcupine/openWakeWord) and better voice (Whisper + neural TTS)
3. Real food/shopping ordering via browser automation (Playwright) with your confirmation
4. Calendar/email/WhatsApp, long-term memory, proactive habits
5. Screen understanding (screenshot -> vision model) for "click that button" control

## Tests
Run tests directly:
```bash
python run.py --test
```
Or with pytest:
```bash
pytest tests
```
