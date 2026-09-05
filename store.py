"""D1 access + the claim/ledger operations for the mock archiver.

Talks to the archiver's OWN D1 database on its OWN Cloudflare account via
`mockdb`, which keeps integer parameters as integers. That matters: the shared
`gori died 2/cf_token.py` stringifies every param, and in SQLite a TEXT value
compares greater than any INTEGER - so `claimed_at < ?` would match every row and
the claim lease would not actually exclude anyone.

Every write is idempotent: `mock_files` has UNIQUE(group_id, msg_id), so a
re-run inserts nothing rather than duplicating - the bug that made the smoke test
upload six files twice.
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import mockdb                                    # noqa: E402

LEASE_MS = 30 * 60 * 1000


class QuotaExhausted(RuntimeError):
    """D1 refused a write because the account hit its daily row-write cap.

    Reads keep working when this happens, which is why the site stays up while a
    writer starts failing. The correct response is to stop cleanly - never to
    keep working without recording, because then a re-run would duplicate every
    forward and every commit.
    """


def _is_quota(e):
    m = str(e).lower()
    return 'row write limit' in m or 'row read limit' in m or 'exceeded' in m


def q(sql, params=None, tries=3):
    """Run one statement, retrying transient failures, classifying quota ones."""
    last = None
    for attempt in range(tries):
        try:
            return mockdb.d1(sql, params)
        except Exception as e:
            last = e
            if _is_quota(e):
                raise QuotaExhausted(str(e)[:200]) from e
            time.sleep(2 * (attempt + 1))
    raise last


def qmeta(sql, params=None, tries=3):
    last = None
    for attempt in range(tries):
        try:
            return mockdb.meta(sql, params)
        except Exception as e:
            last = e
            if _is_quota(e):
                raise QuotaExhausted(str(e)[:200]) from e
            time.sleep(2 * (attempt + 1))
    raise last


def now_ms():
    return int(time.time() * 1000)


def claimable_units(limit=200):
    """Read the whole claimable queue in ONE query.

    Measured: this query shape costs ~2,976 rows_read (93 topics joined to
    groups with a correlated EXISTS over the ledger). Running it once per CLAIM
    is what exhausted D1's 5,000,000 daily row-read cap in under 4 hours. Run
    once per RUN instead and iterate the list in memory: same scheduling, ~50x
    fewer reads.

    Ordering is unchanged - unscanned units first, then least recently scanned.
    """
    stale = now_ms() - LEASE_MS
    refresh_before = now_ms() - 6 * 3600 * 1000
    return q(
        'SELECT t.id, t.group_id, t.topic_id, t.title, t.cursor, t.top_msg, '
        't.dest_topic_id, g.title AS group_title, g.noforwards, g.is_forum '
        'FROM mock_topics t JOIN mock_groups g ON g.group_id = t.group_id '
        'WHERE g.skip = 0 '
        '  AND (t.claimed_at IS NULL OR t.claimed_at < ?) '
        '  AND COALESCE(t.blocked_until, 0) < ? '
        '  AND (t.last_scan_at IS NULL '
        '       OR COALESCE(t.cursor, 0) < COALESCE(t.top_msg, 0) '
        '       OR EXISTS (SELECT 1 FROM mock_files f '
        '                  WHERE f.group_id = t.group_id '
        '                    AND f.topic_id = t.topic_id '
        "                    AND f.status = 'partial') "
        '       OR t.last_scan_at < ?) '
        'ORDER BY (t.last_scan_at IS NOT NULL), COALESCE(t.last_scan_at, 0) ASC, '
        '         t.id ASC LIMIT ?',
        [stale, now_ms(), refresh_before, limit])


def take_lease(unit_id, worker_id):
    """Claim one queued unit. True if this worker won it.

    A pure write with no SELECT: the staleness test lives in the WHERE clause,
    so two workers can never both win the row even though the queue was read
    earlier in the run.
    """
    stale = now_ms() - LEASE_MS
    res = qmeta(
        'UPDATE mock_topics SET claimed_at = ?, claimed_by = ? '
        'WHERE id = ? AND (claimed_at IS NULL OR claimed_at < ?)',
        [now_ms(), worker_id[:80], unit_id, stale])
    return bool((res[1] or {}).get('changes', 0))


def claim(worker_id):
    """Legacy single-unit claim (kept for one-off scripts and tests)."""
    for unit in claimable_units(limit=1):
        if take_lease(unit['id'], worker_id):
            return unit
    return None


def claim_specific(worker_id, unit_id):
    """Claim one named unit. For --unit N: testing and targeted repair.

    Deliberately ignores the lease and the skip flag - the caller named this row
    explicitly, so this is the one path that may override normal scheduling.
    """
    rows = q(
        'SELECT t.id, t.group_id, t.topic_id, t.title, t.cursor, t.top_msg, '
        't.dest_topic_id, g.title AS group_title, g.noforwards, g.is_forum '
        'FROM mock_topics t JOIN mock_groups g ON g.group_id = t.group_id '
        'WHERE t.id = ?', [unit_id])
    if not rows:
        return None
    qmeta('UPDATE mock_topics SET claimed_at = ?, claimed_by = ? WHERE id = ?',
          [now_ms(), worker_id[:80], unit_id])
    return rows[0]


def heartbeat(unit_id, worker_id):
    q('UPDATE mock_topics SET claimed_at = ? WHERE id = ? AND claimed_by = ?',
      [now_ms(), unit_id, worker_id[:80]])


def block_unit(unit_id, hours=12):
    """Quarantine a unit that keeps failing. claim() skips blocked_until.

    Returns the human-readable UTC time it unblocks, for the log line.
    """
    until = now_ms() + hours * 3600 * 1000
    q('UPDATE mock_topics SET blocked_until = ?, claimed_at = NULL, '
      'claimed_by = NULL WHERE id = ?', [until, unit_id])
    return time.strftime('%m-%d %H:%M', time.gmtime(until / 1000))


def release(unit_id, worker_id):
    q('UPDATE mock_topics SET claimed_at = NULL, claimed_by = NULL '
      'WHERE id = ? AND claimed_by = ?', [unit_id, worker_id[:80]])


def set_dest_topic(unit_id, dest_topic_id):
    """Remember the destination topic in D1, not in a local file.

    The smoke test kept this in dest_topics.json, which only works from one
    machine. In D1 it survives a fresh checkout and a GitHub Actions runner.
    """
    q('UPDATE mock_topics SET dest_topic_id = ? WHERE id = ?',
      [dest_topic_id, unit_id])


def done_msg_ids(group_id, topic_id, cursor=0):
    """Ledger state this unit still needs. TWO indexed queries, never a scan.

    Returns (done, partial). `done` is the set of settled msg_ids; `partial`
    maps msg_id -> row for files that reached Telegram but not GitHub (retry the
    commit, NEVER the forward - that is the duplicate this design exists to
    kill).

    Why two queries instead of one with an OR: EXPLAIN QUERY PLAN (see
    _plan_check.py) shows

        WHERE group_id=? AND topic_id=? AND (msg_id > ? OR status='partial')
            -> SCAN mock_files              (reads EVERY row, cost unchanged)
        WHERE group_id=? AND msg_id > ?
            -> SEARCH USING sqlite_autoindex_mock_files_1
        WHERE status='partial'
            -> SEARCH USING idx_mock_files_status

    An OR across two different indexes cannot use either, so the single-query
    version returned few rows while still READING the whole table - and rows_read
    is what D1's 5,000,000/day cap counts. Splitting it makes both halves
    indexed.

    Query 1 covers the only window where a duplicate is possible: with cursor
    pagination Telegram never sends ids at or below the cursor, so the risk is a
    run that forwarded files and died before bump() advanced it.

    Query 2 is NOT scoped by the cursor: a commit owed from any earlier run must
    still be found. It is served by the status index and `partial` is rare, so it
    is cheap.
    """
    above = q('SELECT msg_id, status, tg_ok, gh_ok, dest_msg_id, gh_path '
              'FROM mock_files WHERE group_id = ? AND msg_id > ? '
              'AND topic_id = ?', [group_id, cursor, topic_id])
    owed = q("SELECT msg_id, status, tg_ok, gh_ok, dest_msg_id, gh_path "
             "FROM mock_files WHERE status = 'partial' "
             'AND group_id = ? AND topic_id = ?', [group_id, topic_id])
    done, partial = set(), {}
    for r in above + owed:
        if r['status'] == 'partial' or (r['tg_ok'] and not r['gh_ok']):
            partial[r['msg_id']] = r
        else:
            done.add(r['msg_id'])
    return done, partial


def record(group_id, topic_id, msg_id, **f):
    """Single-row convenience wrapper around record_many()."""
    f.update(group_id=group_id, topic_id=topic_id, msg_id=msg_id)
    record_many([f])


_COLS = ('group_id', 'topic_id', 'msg_id', 'dest_msg_id', 'file_name', 'ext',
         'size', 'caption', 'gh_path', 'status', 'fail_stage', 'tg_ok',
         'gh_ok', 'reason', 'delivery', 'ts')
_UPSERT = (
    'ON CONFLICT(group_id, msg_id) DO UPDATE SET '
    'dest_msg_id = COALESCE(excluded.dest_msg_id, mock_files.dest_msg_id), '
    "gh_path = CASE WHEN excluded.gh_path != '' THEN excluded.gh_path "
    'ELSE mock_files.gh_path END, '
    'status = excluded.status, fail_stage = excluded.fail_stage, '
    'tg_ok = MAX(mock_files.tg_ok, excluded.tg_ok), '
    'gh_ok = MAX(mock_files.gh_ok, excluded.gh_ok), '
    'reason = excluded.reason, delivery = excluded.delivery, '
    'ts = excluded.ts '
    'WHERE mock_files.gh_ok = 0')


def record_many(rows):
    """Insert/update many ledger rows in fewest possible round trips.

    D1 hard limit: 100 bound parameters per query (Cloudflare docs). 16 columns
    per row -> at most 6 rows per statement. The guarded upsert keeps a settled
    row (gh_ok=1) from ever being downgraded by a late write.
    """
    rows = [r for r in rows if r]
    for i in range(0, len(rows), 6):
        part = rows[i:i + 6]
        values = ','.join('(' + ','.join('?' * len(_COLS)) + ')'
                          for _ in part)
        params = []
        for r in part:
            params.extend([
                r.get('group_id'), r.get('topic_id'), r.get('msg_id'),
                r.get('dest_msg_id'),
                (r.get('file_name') or '')[:300], r.get('ext') or '',
                r.get('size') or 0, (r.get('caption') or '')[:1000],
                (r.get('gh_path') or '')[:400], r.get('status') or 'done',
                r.get('fail_stage') or '', 1 if r.get('tg_ok') else 0,
                1 if r.get('gh_ok') else 0,
                (r.get('reason') or '')[:200], r.get('delivery') or 'none',
                now_ms()])
        q(f'INSERT INTO mock_files ({",".join(_COLS)}) VALUES {values} '
          + _UPSERT, params)


def mark_pushed(group_id, msg_ids):
    """Flip forwarded-but-uncommitted rows to done after a successful push."""
    ids = list(dict.fromkeys(msg_ids))
    for i in range(0, len(ids), 90):
        part = ids[i:i + 90]
        ph = ','.join('?' * len(part))
        q(f"UPDATE mock_files SET status = 'done', gh_ok = 1, fail_stage = '' "
          f'WHERE group_id = ? AND gh_ok = 0 AND msg_id IN ({ph})',
          [group_id] + part)


def bump(unit_id, *, archived=0, forwarded=0, ads=0, kind=0, failed_tg=0,
         failed_gh=0, partial=0, bytes_=0, cursor=None, top_msg=None):
    """Incremental counter update - never a recount of the ledger.

    Telegram and GitHub failures are counted apart: a Telegram failure usually
    means the source is protected or the file is oversized, a GitHub failure
    usually means a token, size or rate problem. One number could not tell them
    apart, and they need different responses.

    Same rollup reasoning as the course migration's `up_*` columns: a full
    aggregate per page view exhausted the account's D1 row budget once already.
    """
    sets = ['archived = archived + ?', 'forwarded = forwarded + ?',
            'skipped_ads = skipped_ads + ?', 'skipped_kind = skipped_kind + ?',
            'failed_tg = failed_tg + ?', 'failed_gh = failed_gh + ?',
            'failed = failed + ?',           # kept as the combined total
            'partial = partial + ?', 'bytes = bytes + ?', 'last_scan_at = ?']
    vals = [archived, forwarded, ads, kind, failed_tg, failed_gh,
            failed_tg + failed_gh, partial, bytes_, now_ms()]
    if cursor is not None:
        sets.append('cursor = MAX(COALESCE(cursor,0), ?)')
        vals.append(cursor)
    if top_msg is not None:
        sets.append('top_msg = MAX(COALESCE(top_msg,0), ?)')
        vals.append(top_msg)
    vals.append(unit_id)
    q(f'UPDATE mock_topics SET {", ".join(sets)} WHERE id = ?', vals)


def gate_open():
    """Shared pause switch, same idea as gori-died-2's _dispatch_gate.py."""
    rows = q("SELECT value FROM mock_meta WHERE key = 'mock_paused'")
    return not (rows and str(rows[0]['value']) == '1')


def set_gate(paused):
    q("INSERT INTO mock_meta (key, value) VALUES ('mock_paused', ?) "
      "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
      ['1' if paused else '0'])
