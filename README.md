# sih-watch

Emails the team when an SIH 2026 problem statement's idea count crosses 50, 75, 100, ... (step 25), plus once more at 500/500.
Source: https://sih.gov.in/sih2026PS (column after the PS number, e.g. `22/500`).

## How it decides
- `state.json` stores `last_notified`. A mail goes out only when the current milestone is higher than that, so each milestone mails exactly once.
- If the count jumps past several milestones between checks (48 -> 104), you get ONE mail for 100 that also lists 50 and 75.
- If the fetch/parse fails 6 runs in a row, you get a "watcher failing" mail (once), and a "recovered" mail when it works again.

## Option A: GitHub Actions (no PC needed)
1. New repo (private is fine), push these files.
2. Settings -> Secrets and variables -> Actions -> add `SMTP_USER`, `SMTP_PASS`, `MAIL_TO`.
3. Actions tab -> "SIH watch" -> Run workflow. Check the log shows `SIH26166: NN/500`.
Every 20 min is roughly 2,160 runs/month at under a minute each; private repos get 2,000 free minutes, so for a private repo use `*/30`, or keep it public.

## Option B: local cron (Ubuntu)
    cp .env.example .env && nano .env
    python3 sih_watch.py --test-mail
    python3 sih_watch.py --dry-run
    crontab -e
    */15 * * * * cd $HOME/sih-watch && /usr/bin/python3 sih_watch.py >> watch.log 2>&1

## Flags
`--dry-run` (no mail, no state write) · `--test-mail` · `--html FILE` (parse a saved page)

## Changing the PS / thresholds
Edit `PS_ID`, `START_AT`, `STEP` (workflow env or .env). Delete `state.json` to reset alerts.
