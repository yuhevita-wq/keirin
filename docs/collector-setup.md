# Encrypted Training Data Collection — setup and operating notes

These files add independent data collection to this fork; upstream AI source remains unchanged.

## Before the first run (required on EACH fork)

1. Open **Settings → Secrets and variables → Actions → New repository secret**.
2. Create the secret named `COLLECTOR_PASSPHRASE` with a randomly generated value
   at least 32 characters long. **Keep a private copy.** The archive cannot be
   restored if this key is lost. Never place it in a public file or issue.
3. Open **Actions** and enable GitHub Actions / the **Encrypted Training Data
   Collection** workflow if disabled (GitHub typically disables Actions on forks).
4. Manually run this workflow once for a *completed* recent race date; inspect
   the results in the Actions tab. If incomplete, the job fails closed.
5. When the pilot passes, leave the workflow enabled for daily collection.
   Scheduled time: **06:29 JST** each day, collecting the previous day.

## Data storage

- One standalone SQLite training shard **per day**, built from the inherited parsers.
- Each shard is encrypted locally with GPG AES256. Only its `.sqlite.gpg`
  ciphertext is uploaded to a GitHub Release named `training-data-YYYY-MM`.
- The repository and Releases are PUBLIC; encrypted assets are downloadable by
  others, so the passphrase must be strong and kept private.
- A simple numeric run report (source/date/number of races/rows/errors) is
  uploaded as a 30-day GitHub Actions artifact.
- No plaintext SQLite file is committed, released, or stored as an artifact.
- Recover with `gpg --output collected.sqlite --decrypt file.sqlite.gpg` using
  the original passphrase.

## Limitations

- This is a **daily collector**, NOT a completed historical backfill.
- A past completed date may be passed manually with workflow_dispatch. Automated
  historical backfill needs a separate, verified procedure to avoid silent gaps.
- Some historical source pages may not retain all entry or odds fields.
- Collected final odds are not equivalent to odds captured 15 minutes before a race.
- An incomplete run refuses to publish a shard. Retry the failed date after
  investigating counts/errors; absence of a release asset means NOT collected.
- Stop and reassess any time a data source denies access, rate-limits or changes
  its interface. Do not bypass access restrictions. Respect source provider terms.
- The collector does not place bets or automatically retrain/publish any AI.
- Other workflows inherited in this fork (prediction, results, retraining,
  publishing) are **not** controlled by this collector and should remain disabled
  unless intentionally configured.

## Source code

The collected records use this fork's original collection parsers and schema.
The new runner is `scripts/collect_training_day.py` and the scheduler is
`.github/workflows/encrypted-training-data.yml`.
