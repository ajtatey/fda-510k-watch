# FDA 510(k) watch

Checks the FDA 510(k) database once a day for new clearances from the companies
listed in `companies.json` (currently HeartFlow and Elucid), downloads each
510(k) summary PDF, and sends a notification.

## How it works

1. Queries the [openFDA 510(k) API](https://open.fda.gov/apis/device/510k/) by applicant name.
2. Diffs results against `state/seen.json`.
3. Downloads the summary PDF for each new K number into `summaries/<company>/`,
   alongside a `.json` file with the record metadata.
4. Retries any PDF that was not yet posted on an earlier run.
5. Posts to Slack (and/or email) when something new appears.

openFDA refreshes roughly weekly, so a clearance typically shows up here a few
days after the FDA decision date.

De Novo grants (DEN numbers) also appear in the openFDA 510(k) feed. The script
recognises them and fetches the De Novo decision summary from its own FDA path.

## Local setup

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python fda_510k_watch.py --backfill      # first run: record existing clearances, no alerts
python fda_510k_watch.py --dry-run       # see what would be new without writing anything
python fda_510k_watch.py                 # normal daily run
```

## Notifications

Set whichever of these you want. With none set, alerts print to stdout.

| Variable | Purpose |
|---|---|
| `SLACK_WEBHOOK_URL` | Slack incoming webhook |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`, `EMAIL_TO` | Email fallback |
| `OPENFDA_API_KEY` | Optional, raises openFDA rate limits |

## Scheduling with GitHub Actions

`.github/workflows/daily.yml` runs the script every day at 13:00 UTC and commits
any new state and PDFs back to the repo.

1. Push this directory to a GitHub repository.
2. Add `SLACK_WEBHOOK_URL` (and any email secrets) under Settings → Secrets and variables → Actions.
3. Run the workflow once by hand from the Actions tab to confirm it works.

## Adding a company

Append an entry to `companies.json`:

```json
{"name": "cleerly", "applicant_query": "cleerly"}
```

`applicant_query` is matched against the openFDA `applicant` field, so a short
distinctive word is enough to catch name variants like "HeartFlow, Inc.".
