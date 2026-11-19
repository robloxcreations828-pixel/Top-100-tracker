# 🚀 Roblox Trend Tracker

Runs 24/7 for free on GitHub. About every 5 minutes it checks two lists:
- **Top Trending:** Roblox's top 100 trending games
- **New & Most Played:** the most-played games created in the last 30 days. Roblox's own lists only go ~100 deep, so the tracker pools Roblox's Trending, Most Played and Up-and-Coming lists across 10 countries and computer + phone (plus the home page), keeps the ones under 30 days old and ranks them by live players. This list can hold fewer than 100 games.

Both lists use the same alert rules, and a game in both lists only alerts once.

**New-entry alerts:** you also get an alert as soon as a game **under 10 days old** enters either list, even before it spikes. When a **new game enters the top 100** and its player count **grows massively in a short time**, it alerts you and tells you **how many other games have a similar name**.

- **Alerts:** Discord, email, and phone push notifications (through the free ntfy app)
- **Dashboard:** a live web page with the top 100, 1-hour growth, sparklines, new entries and the alert history

## What counts as a spike (default settings)

A game sends an alert when **all** of these are true:
1. It is **new** to the top 100: it wasn't in the list at any time in the last 7 days, and it entered within the last 24 hours.
2. It has at least **1,000 players**.
3. The game was **uploaded (created) in the last 30 days**.
4. Its players grew **+50% or more within 60 minutes** (at least +500 players), **or** they have **doubled since it entered** the top 100.

Each game sends at most one alert every 6 hours. The first run only records a baseline, so games that are already in the list don't count as new.

**Similar names:** the tracker searches Roblox for the game's name, after removing tags like `[UPDATE]` and emojis. It then counts the results whose names closely match, and also checks how many look-alikes are in the top 100. Roblox search only returns the top ~120 results, so the count is "similar games found in search", not every copy on the platform.

---

## Setup (about 15 minutes)

### 1. Put the code on GitHub
1. Make a free account at github.com.
2. Create a **new repository** (for example `roblox-trend-tracker`) and set it to **Public**. Public repos get unlimited free running time; private ones would run out of free minutes.
3. Upload every file from this folder, keeping the `.github/workflows` folder. To do that, click **Add file → Upload files** and drag the files in.

### 2. Turn on alerts
Go to **Settings → Secrets and variables → Actions → New repository secret**, then add the secrets for the alerts you want. You can skip any you don't need.

| Alert | Secret name | Value |
|---|---|---|
| Discord | `DISCORD_WEBHOOK_URL` | In Discord, open the channel's settings and go to **Integrations → Webhooks → New Webhook → Copy URL** |
| Phone push | `NTFY_TOPIC` | Pick a long, hard-to-guess name such as `ben-roblox-trends-8k2j9`. Then install the **ntfy** app (iOS/Android) and subscribe to that same name |
| Email | `SMTP_HOST` | `smtp.gmail.com` |
| | `SMTP_PORT` | `587` |
| | `SMTP_USER` | your Gmail address |
| | `SMTP_PASS` | a Gmail **App Password**. Create one at myaccount.google.com/apppasswords (2-step verification has to be on). Your normal password won't work |
| | `EMAIL_TO` | the address that should receive alerts |

### 3. Turn on the dashboard
Go to **Settings → Pages**. Under Source, choose **Deploy from a branch**, then pick branch **main** and folder **/docs**, and click Save.
Your dashboard will be at `https://YOUR-USERNAME.github.io/REPO-NAME/`

### 4. Start it
1. Open the **Actions** tab and click **Enable workflows** if GitHub asks.
2. Click **Track Roblox trending → Run workflow**. This first run saves the baseline.
3. To check your alerts work, click **Run workflow** again and tick **"Send a test alert"**. You should get a Discord message, a push notification and an email.

After that it runs automatically, day and night.

---

## Tuning (optional)
To change the defaults, go to **Settings → Secrets and variables → Actions → Variables** and add any of these:

| Variable | Default | Meaning |
|---|---|---|
| `SORT_ID` | `top-trending` | Which Roblox list to watch: `top-trending`, `top-playing-now` or `up-and-coming` |
| `MIN_PLAYERS` | `1000` | Ignore games smaller than this |
| `SPIKE_PCT` | `50` | % growth needed inside the time window |
| `SPIKE_WINDOW_MIN` | `60` | The time window, in minutes |
| `SPIKE_MIN_GAIN` | `500` | Minimum number of extra players |
| `NEW_LIST_MAX_AGE_DAYS` | `30` | Age limit for the New & Most Played list |
| `ENTRY_ALERT_MAX_AGE_DAYS` | `10` | Alert when a game this young enters a list (`0` = off) |
| `MAX_AGE_DAYS` | `30` | Only alert for games created within this many days (`0` = any age) |
| `WATCH_HOURS` | `24` | How long after entering the list a game still counts as "new" |

If you get too many alerts, raise `SPIKE_PCT` or `MIN_PLAYERS`. If you get too few, lower them.

## Good to know
- GitHub's timer is "best effort", so runs sometimes start 5–15 minutes late when GitHub is busy.
- GitHub pauses scheduled jobs in repos with no activity for 60 days. If that happens you'll get an email, and one click re-enables it.
- History is kept on a separate `data` branch. It's overwritten every run, so the repo doesn't grow.
- The tracker uses Roblox's public (unofficial) website APIs. If Roblox changes them, the run fails and shows a red ❌ in the Actions tab, and you'll get an email from GitHub.

## Files
- `tracker.py`: the tracker itself (Python, no installs needed)
- `.github/workflows/track.yml`: runs it every 5 minutes
- `docs/index.html`: the dashboard
