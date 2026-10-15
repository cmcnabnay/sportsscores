-- Supabase schema for sportsscores. Run once in the Supabase SQL editor.
--
-- matches:        one row per match (written by fetch_fixtures.py).
-- fixtures_meta:  single row holding everything else the page needs
--                 (leagues, standings, attendance_tables, playoffs,
--                 matrix_last_fetch, updated) as one jsonb document.
--
-- Reads are public (publishable/anon key, used by matches.html).
-- There are no write policies: only the secret key, which bypasses RLS,
-- can write.

create table matches (
  -- Stable ID: first 16 hex chars of sha1("league|teamA|teamB|date") with
  -- the two team names sorted - the same identity fetch_fixtures.py's
  -- _match_identity() uses to merge runs. See match_id() in that script.
  match_id     text primary key,
  league       text not null,
  home         text not null,
  away         text not null,
  score        text,
  date         date,
  time         text,            -- local kick-off "HH:MM"
  utc          timestamptz,
  venue        text,
  attendance   integer,
  ot           boolean not null default false,
  group_name   text,            -- "group" in the JSON (reserved word in SQL)
  round        text,
  referee      text,
  status       text,
  updated_at   timestamptz not null default now()
);

create index matches_league_date on matches (league, date);
create index matches_date on matches (date);

create table fixtures_meta (
  id          int primary key default 1 check (id = 1),
  data        jsonb not null,
  updated_at  timestamptz not null default now()
);

-- Bump updated_at whenever a row actually changes.
create function touch_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

create trigger matches_touch before update on matches
  for each row when (old.* is distinct from new.*)
  execute function touch_updated_at();

create trigger fixtures_meta_touch before update on fixtures_meta
  for each row when (old.* is distinct from new.*)
  execute function touch_updated_at();

alter table matches enable row level security;
alter table fixtures_meta enable row level security;
create policy "public read" on matches for select to anon using (true);
create policy "public read" on fixtures_meta for select to anon using (true);
