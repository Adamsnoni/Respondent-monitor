# Respondent Monitor

A Playwright-driven Python scraper that checks the [Respondent.io Public Projects](https://www.respondent.io/research-projects) directory for new studies. It remembers which studies it has already seen using a local SQLite database and pushes real-time Telegram alerts for genuinely new opportunities.

## Environment Variables
- `TELEGRAM_BOT_TOKEN`: **(Required)** The token for your Telegram bot.
- `TELEGRAM_CHAT_ID`: **(Required)** The chat ID to send notifications to.
- `RESPONDENT_BROWSE_URL`: (Optional) Target URL. Defaults to the public projects directory.
- `HEADLESS`: (Optional) `1` for Background (No GUI), `0` to launch a visible Chrome window locally. Defaults to `1`.
- `MAX_STUDIES_PER_RUN`: (Optional) How many projects to scrape per run. Defaults to `40`.
- `DB_PATH`: (Optional) Path to SQLite DB. Mounts to `/data/respondent_studies.db` by default.

## Running Locally

1. Make sure Python 3.8+ is installed.
2. Run the included bash script to install dependencies and instantly trigger a manual scrape:
   ```bash
   bash run.sh
   ```
*(Note: Requires valid Telegram credentials exported in your terminal session to send alerts)*

## Pushing to GitHub

Use standard Git commands to push your project. The `.gitignore` is pre-configured to exclude your local DB, virtual environments, and `.env` credentials.
```bash
git init
git add .
git commit -m "Initial commit of Respondent Monitor"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
git push -u origin main
```

## Deploying to Render

This repository includes a `render.yaml` configuration for seamless automated deployments on [Render](https://render.com).

1. Go to your Render Dashboard and create a new **Blueprint instance** via **New -> Blueprint**.
2. Connect the GitHub repository you just pushed.
3. Render will auto-detect the `render.yaml` file and provision your Cron Job.
4. **Environment Variables**: Once the blueprint starts, go to the Service's "Environment" tab to paste your `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (they are deliberately un-synced for security). 
5. *(Note on Disks)*: Render's cron type currently restricts attaching persistent disks. If your data wipes on every cron run (duplicate alerts), change `type: cron` to `type: worker` inside `render.yaml` and loop your script inside Python.

## Troubleshooting

- **Telegram notifications not sending**: Ensure your `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are valid. If the bot hasn't messaged you before, you must send it a `/start` message first to open the channel.
- **Playwright missing browsers error**: To fix broken Chrome dependencies locally, run `playwright install chromium` and `playwright install-deps chromium`. (Our `run.sh` does this automatically).
- **Out of Memory on Render**: The Playwright scraper spins up a real Chrome instance taking ~300MB RAM. Monitor it cleanly to make sure it doesn't crash a 512MB RAM free-tier limit.
