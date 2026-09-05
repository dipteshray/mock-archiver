-- Mock archiver tracking tables.
--
-- Lives in its OWN D1 database (`mock-archive`) on its OWN Cloudflare account,
-- NOT alongside the gori-died-2 migration. The free tier's 100,000 daily row
-- writes are counted per ACCOUNT, and the migration fleet uses all of them
-- (18,261 ledger inserts in one day), which stopped every mock write with error
-- 7500 while reads carried on. Separate account = separate quota = neither
-- project can starve the other.
--
-- The `mock_` prefix is kept so a dump from the old shared database still
-- restores cleanly here.
--
-- Shape mirrors the migration's on purpose: one row per unit of work with a
-- claim lease, one ledger row per delivered file, counters rolled up
-- incrementally so the site never scans the ledger.

-- The 14 source groups. `n` is the number in the user's original list.
CREATE TABLE IF NOT EXISTS mock_groups (
  group_id      INTEGER PRIMARY KEY,   -- Telegram channel id (positive form)
  n             INTEGER,
  title         TEXT,
  username      TEXT,
  is_forum      INTEGER DEFAULT 0,
  noforwards    INTEGER DEFAULT 0,     -- 1 = must download+reupload
  skip          INTEGER DEFAULT 0,     -- 1 = excluded (PiroMocks)
  n_topics      INTEGER DEFAULT 0,
  added_at      INTEGER,
  updated_at    INTEGER
);

-- One row per unit of work: a whole flat channel, or one topic of a forum.
-- topic_id = 0 means "the channel itself, no topics".
CREATE TABLE IF NOT EXISTS mock_topics (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id      INTEGER NOT NULL,
  topic_id      INTEGER NOT NULL DEFAULT 0,
  title         TEXT,                  -- topic title, or group title when flat
  closed        INTEGER DEFAULT 0,
  cursor        INTEGER DEFAULT 0,     -- highest source msg id processed
  top_msg       INTEGER DEFAULT 0,     -- highest msg id that exists at source
  dest_topic_id INTEGER,               -- topic created in DEST
  archived      INTEGER DEFAULT 0,     -- committed to GitHub
  forwarded     INTEGER DEFAULT 0,     -- delivered to DEST
  skipped_ads   INTEGER DEFAULT 0,
  skipped_kind  INTEGER DEFAULT 0,     -- video/photo/text: not archivable
  failed        INTEGER DEFAULT 0,     -- total, = failed_tg + failed_gh
  failed_tg     INTEGER DEFAULT 0,     -- Telegram refused the delivery
  failed_gh     INTEGER DEFAULT 0,     -- GitHub refused the commit
  partial       INTEGER DEFAULT 0,     -- in Telegram, NOT yet on GitHub
  bytes         INTEGER DEFAULT 0,
  claimed_at    INTEGER,
  claimed_by    TEXT,
  blocked_until INTEGER,
  last_scan_at  INTEGER,
  UNIQUE (group_id, topic_id)
);

-- The ledger. UNIQUE(group_id, msg_id) is the whole point: the smoke test
-- re-uploaded 6 files across three attempts because nothing recorded "already
-- done". A constraint makes re-runs idempotent in the database, not by
-- convention.
--
-- Delivery happens in two stages - Telegram first, then GitHub - so a row records
-- BOTH outcomes. Without that split a GitHub failure looked identical to a
-- Telegram failure, and worse: re-running would re-forward a file that was
-- already delivered, duplicating it in the destination group.
CREATE TABLE IF NOT EXISTS mock_files (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id      INTEGER NOT NULL,
  topic_id      INTEGER NOT NULL DEFAULT 0,
  msg_id        INTEGER NOT NULL,
  dest_msg_id   INTEGER,               -- message id in DEST
  file_name     TEXT,
  ext           TEXT,
  size          INTEGER DEFAULT 0,
  caption       TEXT,
  gh_path       TEXT,                  -- path committed in the archive repo
  status        TEXT DEFAULT 'done',   -- done | partial | ad | skipped | failed
  fail_stage    TEXT,                  -- '' | telegram | github
  tg_ok         INTEGER DEFAULT 0,     -- 1 = delivered to the group
  gh_ok         INTEGER DEFAULT 0,     -- 1 = committed to the repo
  reason        TEXT,                  -- why skipped/failed, or ad rule hit
  delivery      TEXT,                  -- forwarded | uploaded | none
  ts            INTEGER,
  UNIQUE (group_id, msg_id)
);

-- Indexes are row-writes too on D1 (each index entry is written on INSERT),
-- so mock_files carries the minimum: the UNIQUE constraint (dedupe) and the
-- status index the status page filters on. group_id is covered by the UNIQUE
-- prefix; ts/name/stage lookups are not worth 3 extra row-writes per file.
CREATE INDEX IF NOT EXISTS idx_mock_files_status ON mock_files(status);
CREATE INDEX IF NOT EXISTS idx_mock_topics_claim ON mock_topics(claimed_at, cursor, top_msg);


-- Same key/value table used as the pause switch (`mock_paused=1`).
CREATE TABLE IF NOT EXISTS mock_meta (
  key           TEXT PRIMARY KEY,
  value         TEXT
);

