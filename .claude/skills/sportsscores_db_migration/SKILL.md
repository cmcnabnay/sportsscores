---
name: sportsscores_db_migration
description: Migrate sportsscores' fixtures.json into Supabase so matches.html reads from the database, fetch_fixtures.py writes to it, and fixtures.json stops being committed/deployed to Vercel. Use when the user says Vercel deployment storage has grown too large, or invokes /sportsscores_db_migration.
---

# sportsscores: move fixtures.json into Supabase

## Background (as of 2026-10-13)

- `fetch-fixtures.service` (systemd) runs `fetch_and_push.sh` about every 11 minutes. The script runs
  `fetch_fixtures.py`, then commits and pushes `fixtures.json` whenever it has changed.
- Every push triggers a Vercel deployment. Unchanged files are shared between deployments, but each
  deployment stores a new copy of `fixtures.json` (1.3–1.8 MB). Between 2026-09-21 and 2026-10-13,
  1,983 of 1,988 commits touched only `fixtures.json`, and Vercel deployment storage grew from 1.75 GB
  to 3.12 GB. The `.git` directory is only about 23 MB, so git is not the problem.
- The user **deliberately kept the commits** because they keep their GitHub contribution graph
  green. They chose to postpone this migration until storage got too high. Respect that: see
  "Keeping the contribution graph green" below and ask which option they want before removing the
  commits.

### Re-verify first

Run these before changing anything, because the code may have changed since this plan was written:

```bash
git log --since="<last check date>" --format=%h --name-only | grep -v '^[0-9a-f]\{7,\}$' | grep . | sort | uniq -c
ls -la fixtures.json && du -sh .git
grep -n "def load_existing\|def save\|OUTPUT_FILE" fetch_fixtures.py
grep -n "fixtures.json" matches.html
```

### Code facts this plan relies on (re-check them)

- The page is **`matches.html`**. There is no `fixtures.html`. `vercel.json` rewrites `/` to
  `/matches.html`. `loadFixtures()` fetches `fixtures.json?t=<now>` and reads
  `data.matches/leagues/standings/attendance_tables/playoffs`. It re-fetches every 5 minutes.
- `fetch_fixtures.py` only reads and writes the file in two places: `load_existing()` and
  `save(data)`, both around line 4878. All merging is done in memory on the whole document, and
  matches have **no stable ID**. So store the whole document, not normalized rows.
- Top-level keys: `updated`, `leagues` (~37), `matches` (~5.5k, ~1.2 MB), `standings`,
  `attendance_tables`, `playoffs`, `matrix_last_fetch`.
- The script uses `urllib` (`urlopen`). Don't add the `supabase` Python package; call PostgREST
  directly.
- `.env.local` is gitignored (`.env*`). Put secrets there.

## Steps

### 1. Supabase setup
Ask the user for the project URL, the publishable (anon) key and the secret (service_role) key, or
ask them to create a project first. Then run:

```sql
create table fixtures_doc (
  id int primary key default 1 check (id = 1),
  data jsonb not null,
  updated_at timestamptz not null default now()
);
alter table fixtures_doc enable row level security;
create policy "public read" on fixtures_doc for select to anon using (true);
-- No write policies: only the secret key (which bypasses RLS) can write.
```

Add `SUPABASE_URL` and `SUPABASE_SECRET_KEY` to `.env.local`. **Never** put the secret key in
`matches.html` or in git. The publishable key is safe to embed in the page, because RLS only allows
reads.

### 2. Seed the table
Write a one-off script, in the scratchpad rather than the repo, that upserts the current
`fixtures.json` into row `id=1`:
`POST {SUPABASE_URL}/rest/v1/fixtures_doc`
- Headers: `apikey` and `Authorization: Bearer <secret>`,
  `Content-Type: application/json`, `Prefer: resolution=merge-duplicates,return=minimal`
- Body: `{"id": 1, "data": <doc>, "updated_at": <now>}`

### 3. fetch_fixtures.py
- Load `.env.local` without adding a new dependency: parse the `KEY=VALUE` lines by hand unless
  python-dotenv is already used.
- `load_existing()`: `GET {URL}/rest/v1/fixtures_doc?id=eq.1&select=data`. Use `rows[0]["data"]`.
  Keep the existing `setdefault`s. If Supabase can't be reached, fall back to the local
  `fixtures.json`. If the table is empty, return the existing empty-document default.
- `save(data)`: upsert as in step 2. Skip the write if the data is unchanged apart from `updated`.
  That means comparing against the document as loaded, which keeps today's only-commit-on-change
  behaviour. Keep writing the local `fixtures.json` as a backup and cache.
- Raise or log loudly if the upsert fails, so the systemd journal shows it.
- Build on the user's uncommitted work in `fetch_fixtures.py`. Never reset or discard it.

### 4. matches.html
- In `loadFixtures()`, fetch
  `${SUPABASE_URL}/rest/v1/fixtures_doc?id=eq.1&select=data,updated_at` with the header
  `apikey: <publishable key>`. Then set `data = rows[0].data`. Everything after that point stays
  the same.
- Remove the `?t=` cache-buster.
- Replace the "run fetch_fixtures.py / python3 -m http.server" error text with a generic "Could not
  load fixtures" message.

### 5. Run both in parallel
- Leave the git commit/push in `fetch_and_push.sh` for a few cycles. Deploy the new `matches.html`.
- Confirm the live site shows current scores coming from Supabase. Check the network tab, or
  compare `updated_at` with the latest run.

### 6. Stop the deployments that cause the growth
Only do this after asking the user (see the next section).
- Remove the `git add`, `git commit` and `git push` lines from `fetch_and_push.sh`. Consider
  renaming the script and updating the systemd unit to match.
- Run `git rm --cached fixtures.json` and add `fixtures.json` to `.gitignore`.
- Add a `.vercelignore` containing `fixtures.json`, `fetch_fixtures.py`, `fetch_and_push.sh`,
  `diagnose.py`, `query`, `*.txt`, `.claude/` and `__pycache__/`. The deployment only needs
  `matches.html` and `vercel.json`.

### 7. Recover the existing storage
- Stopping deployments stops the growth, but the space stays used until old deployments are
  removed.
- Recommend setting a deployment retention policy in Vercel → Project → Settings.
- Alternatively, bulk-delete old preview and production deployments with `vercel rm`. **Deleting
  can't be undone: list what will be deleted and get explicit confirmation first.** Never delete
  the current production deployment.

## Keeping the contribution graph green

Ask the user which of these they want before doing step 6:
- **A. Separate repo for the heartbeat commits (recommended).** Have the systemd job commit a tiny
  log to a separate GitHub repo that isn't linked to Vercel. For example, append a line to
  `runs.log` with the timestamp and match count. The contribution graph stays green and nothing
  deploys. Commits only count toward the graph if the author email is linked to the user's GitHub
  account and the commits land on the repo's default branch.
- **B. Keep committing to this repo, but skip the Vercel builds.** Commit something small, such as
  a `last_run.txt`. Then set Vercel's Ignored Build Step to skip builds when only that file
  changed, for example:
  `git diff --quiet HEAD^ HEAD -- . ':!last_run.txt'`.
- **C. Stop the heartbeat commits altogether.**

## Notes
- **Egress:** each viewer downloads the whole document every 5 minutes, about 160 KB gzipped.
  That's fine on Supabase's free tier (5 GB/month) at low traffic. Revisit if traffic grows.
- **Free-tier pausing:** Supabase pauses inactive free projects. Regular writes should count as
  activity, but mention it to the user.
- **Possible later step:** a `matches` table with a stable key, so the page can load only a date
  range. This needs a stable match ID in `merge_league_matches` first, so keep it out of this
  migration.
