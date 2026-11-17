# Appointment watch bot – Zulassungsstelle LRA Reutlingen

Watches the LRA Reutlingen smartCJM calendar for the 3rd service on the first page ("option 3") and **books the first free appointment automatically** with your name and email. You get a push notification on your tablet when it finds a slot and when it books.

## Why it runs on a phone and not on GitHub

The booking site's Cloudflare blocks datacenter IP addresses. That was tested: GitHub Actions runners get *"Sorry, you have been blocked"*. A phone on your home Wi‑Fi has a normal home IP, so it gets through. The code lives in this repo, and the phone pulls updates automatically.

The rotating link and the 20‑minute timeout don't matter. Every check starts a fresh session from the calendar `uid`.

## Setup (about 15 minutes, once)

**On the tablet**
1. Optional: install the **ntfy** app if you want push alerts. The bot books without it.

**On the spare Android phone** (on home Wi‑Fi, plugged into a charger)
1. Install **Termux** and **Termux:Boot** from **F‑Droid** (https://f-droid.org). The Play Store versions are outdated.
2. Open Termux:Boot once, then close it. This lets the bot restart after a reboot.
3. Open Termux and paste:
   ```
   curl -fsSL https://raw.githubusercontent.com/ahmadabouelainein/Appointment-watch-bot/ccr-4b08f47b-46ho73/setup.sh | bash
   ```
   It asks for first name, last name, email (and optionally phone). These stay **only on the phone** in `config.env`. They are never uploaded.
4. It prints a **notification topic** like `lra-rt-3f9c…`. In the ntfy app on the tablet, tap **+** and subscribe to that topic.
5. It then does a test run that **does not book**, and sends you a "Discovery finished" notification with a zip of what it saw.
6. Start the bot:
   ```
   cd ~/Appointment-watch-bot && ./run.sh
   ```
   Leave Termux open, or swipe it away: it holds a wake lock. In Android settings, set **Battery → Termux → Unrestricted**, or the bot may get killed.

## What happens

- Every ~75 s (randomised), the bot opens a fresh session, selects option 3, and checks for free slots.
- When it finds a slot, you get an urgent notification with a link to book by hand as a backup. The bot then fills in your details and books the first slot.
- If the booking succeeds, you get a **BOOKED ✅** notification and the bot stops. Check your email: the office may require you to click a confirmation link.
- If the booking form asks for something the bot doesn't have, it alerts you to book manually.

## Options (`config.env` on the phone)

| key | meaning |
| --- | --- |
| `SERVICE_INDEX` | which service on the first page (default `3`) |
| `SERVICE_NAME` | instead of the index, part of the service name, e.g. `Neuzulassung` |
| `NOT_BEFORE` / `NOT_AFTER` | only accept slots in this date range (`YYYY-MM-DD`) |
| `AUTO_BOOK` | `1` = book automatically, `0` = alert only |
| `POLL_SECONDS` | time between checks (please don't go much below 60) |

Useful commands on the phone: `python bot.py discover` (test run, never books), `tail -f bot.log` (when started at boot), `rm state.json` (watch again after a booking).
