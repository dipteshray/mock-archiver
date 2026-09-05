"""The mock archiver worker.

    python archive.py                 # run until the time budget expires
    python archive.py --units 2       # stop after N units (testing)
    python archive.py --dry           # decide everything, change nothing

For each unit of work (a flat channel, or one topic of a forum):
  1. claim it in D1 (lease, so two runners never collide)
  2. walk its messages newest-first, skipping ids already in the ledger
  3. drop adverts and anything that is not a study document
  4. download the file
  5. forward it to DEST - or re-upload when the source is content-protected
  6. commit it to GitHub as <group>/[<topic>/]<file title [id]>/<file> + caption.txt
  7. record it in `mock_files` and bump the unit's counters
  8. release the claim

Idempotent by construction: `mock_files` is UNIQUE(group_id, msg_id) and step 2
reads that set first, so re-running never duplicates a forward or a commit.
"""
import asyncio
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from telethon import TelegramClient, errors            # noqa: E402
from telethon.sessions import StringSession            # noqa: E402
from telethon.tl.functions.messages import (           # noqa: E402
    CreateForumTopicRequest, ForwardMessagesRequest)

import adfilter                                         # noqa: E402
import config                                           # noqa: E402
import fast_telethon
import ghbatch
import ghcommit
import layout                                           # noqa: E402
import sources                                          # noqa: E402
import store                                            # noqa: E402

BUDGET_MIN = float(os.environ.get('MOCK_BUDGET_MIN', '45'))
MAX_UNITS = int(os.environ.get('MOCK_MAX_UNITS', '0'))
SCAN_LIMIT = int(os.environ.get('MOCK_SCAN_LIMIT', '1000'))
MAX_FILE_MB = float(os.environ.get('MOCK_MAX_FILE_MB', '90'))
DRY = '--dry' in sys.argv
ONLY_UNIT = None                 # --unit N: work one specific row (test/repair)
for i, a in enumerate(sys.argv):
    if a == '--units' and i + 1 < len(sys.argv):
        MAX_UNITS = int(sys.argv[i + 1])
    if a == '--unit' and i + 1 < len(sys.argv):
        ONLY_UNIT = int(sys.argv[i + 1])

START = time.time()
WORKDIR = os.path.join(BASE, '_archive_work')      # git staging clone
FWD_CHUNK = 50          # ids per ForwardMessagesRequest (TL cap is 100)
DL_CONCURRENCY = 3      # parallel downloads on the one session
FAST_DL_MB = 4          # FastTelethon for files this size and up
WORKER = f'archiver-{os.getpid()}'


def remaining():
    return BUDGET_MIN * 60 - (time.time() - START)


def log(*a):
    print(*a, flush=True)


async def get_client():
    """Prefer TELEGRAM_SESSION (CI) over the local .session file.

    A file session cannot be committed, so GitHub Actions must get the
    StringSession from a secret. Locally the file is the convenient path.
    """
    ss = os.environ.get('MOCK_SESSION') or os.environ.get('TELEGRAM_SESSION')
    if ss:
        client = TelegramClient(StringSession(ss), config.API_ID, config.API_HASH)
    else:
        client = TelegramClient(config.SESSION_NAME, config.API_ID,
                                config.API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit('[FATAL] session is not authorized')
    return client


async def warm_entity_cache(client):
    """Populate the session's entity cache by listing all dialogs.

    Without this, a FRESH session (every CI run) cannot resolve a bare id like
    `get_entity(3351594809)` - Telethon raises "Could not find the input entity"
    because the id alone does not say whether it is a user, chat or channel, and
    an empty session has no cache to answer from. The local runs only worked
    because the .session file had cached every source group during resolution.

    One pass over the account's dialogs fixes it permanently for the run: each
    dialog stores its full entity (id, hash, type) in the session, after which
    get_entity by id works for every chat the account is in.
    """
    n = 0
    async for _ in client.iter_dialogs():
        n += 1
    log(f'[WARM] entity cache populated from {n} dialog(s)')


async def ensure_dest_topic(client, dest, unit):
    """Get (or create) the `<topic> - <group>` topic in DEST.

    The id is stored in D1, not a local file, so a fresh runner reuses the same
    topic instead of creating a duplicate every wave.
    """
    if unit.get('dest_topic_id'):
        return unit['dest_topic_id']
    title = layout.dest_topic_title(
        unit['group_title'], unit['title'] if unit['topic_id'] else None)
    # NOTE: peer=, not channel=, and random_id is mandatory. Both were live bugs.
    res = await client(CreateForumTopicRequest(
        peer=dest, title=title,
        random_id=int.from_bytes(os.urandom(8), 'big', signed=True)))
    tid = None
    for u in res.updates:
        msg = getattr(u, 'message', None)
        if msg is not None:
            tid = msg.id
            break
    if tid:
        store.set_dest_topic(unit['id'], tid)
        log(f'   [TOPIC] created {title!r} -> {tid}')
    return tid


async def forward_chunk(client, dest, unit, src_ent, members, tid_box, stats,
                        attempt=1):
    """Forward up to FWD_CHUNK messages in ONE request, hidden author.

    FloodWait is handled inside this function on purpose: the ledger write only
    happens AFTER the request succeeds, so a chunk that never went through is
    simply re-forwarded next run by the cursor - a phantom `partial` row (which
    would promise a GitHub commit for a file that was never delivered) is the
    one thing this must never produce. On a second floodwait the chunk is split
    in half; on budget exhaustion it raises and the cursor stays put.
    """
    ids = [m['msg'].id for m in members]
    while True:
        try:
            rids = [int.from_bytes(os.urandom(8), 'big', signed=True)
                    for _ in ids]
            res = await client(ForwardMessagesRequest(
                from_peer=src_ent, to_peer=dest, id=ids,
                top_msg_id=tid_box['tid'], drop_author=True, random_id=rids))
            break
        except (errors.FloodWaitError, errors.SlowModeWaitError) as e:
            wait = min(e.seconds + 1, max(1, int(remaining())))
            if wait >= remaining() - 60:
                raise          # chunk was NOT recorded - cursor retries it
            log(f'   [FLOODWAIT] forwarding {len(ids)}: sleeping {wait}s')
            await asyncio.sleep(wait)
            if attempt >= 2 and len(ids) > 1:
                half = len(ids) // 2
                await forward_chunk(client, dest, unit, src_ent,
                                    members[:half], tid_box, stats, attempt + 1)
                await forward_chunk(client, dest, unit, src_ent,
                                    members[half:], tid_box, stats, attempt + 1)
                return
            attempt += 1

    # Map delivered messages back to source members by position. If Telegram
    # returned fewer than sent, leftovers keep dest_msg_id None - the file IS
    # in the group either way, only the link is unknown.
    new_ids = []
    for u in getattr(res, 'updates', []):
        m = getattr(u, 'message', None)
        if m is not None:
            new_ids.append(m.id)

    rows = []
    for i, m in enumerate(members):
        rows.append({
            'group_id': unit['group_id'], 'topic_id': unit['topic_id'],
            'msg_id': m['msg'].id,
            'dest_msg_id': new_ids[i] if i < len(new_ids) else None,
            'file_name': m['fn'], 'ext': m['ext'], 'size': m['size'],
            'caption': m['caption'], 'gh_path': m['gh_path'] + '/' + m['fn'],
            'status': 'partial', 'fail_stage': 'github',
            'tg_ok': 1, 'gh_ok': 0, 'delivery': 'forwarded',
        })
    # Durability point: partial rows land BEFORE any download starts, so a
    # crash here means "retry the commit", never "forward again".
    store.record_many(rows)
    stats['forwarded'] += len(members)
    log(f'   [FWD] {len(members)} file(s) forwarded in one request')


async def download_member(client, unit, m, sem, git, pending_push, stats,
                          resumed=False):
    """Download one file straight into the git work tree, then stage it."""
    async with sem:
        dl_path = os.path.join(BASE, '_tmp_dl',
                            f'{m["msg"].id}_{m["safe_fn"]}')
        try:
            os.makedirs(os.path.dirname(dl_path), exist_ok=True)
            if m['size'] >= FAST_DL_MB * 1024 * 1024:
                # Parallel-connection download - pays off on bigger files.
                with open(dest_path, 'wb') as fh:
                    await fast_telethon.download_file(
                        client, m['msg'].document, fh)
            else:
                await client.download_media(m['msg'], file=dl_path)
            if not os.path.exists(dl_path):
                raise RuntimeError('download produced no file')
            git.add(m['gh_path'], dl_path)   # moves tmp into git
            if m['caption']:
                git.add(m['gh_dir'] + '/caption.txt', m['caption'].encode())
            pending_push.append((unit['group_id'], m['msg'].id))
            stats['bytes'] += m['size']
            if resumed:
                stats['recovered'] += 1
        except errors.FloodWaitError as e:
            wait = min(e.seconds + 1, max(1, int(remaining())))
            if wait < remaining() - 60:
                await asyncio.sleep(wait)
                return await download_member(client, unit, m, sem, git,
                                             pending_push, stats, resumed)
            store.record(
                unit['group_id'], unit['topic_id'], m['msg'].id,
                file_name=m['fn'], ext=m['ext'], size=m['size'],
                caption=m['caption'], gh_path=m['gh_path'] + '/' + m['fn'],
                status='partial', fail_stage='github', tg_ok=1, gh_ok=0,
                reason=f'download floodwait {e.seconds}s',
                delivery=m['delivery'])
            stats['partial'] += 1
            log(f'   [DL FAIL] {m["fn"][:44]} floodwait {e.seconds}s - '
                f'commit deferred')
        except Exception as e:
            # The file IS in the destination group already (tg_ok=1). Recording
            # partial - not failed - is what makes the next run retry the
            # commit only, never the forward.
            store.record(
                unit['group_id'], unit['topic_id'], m['msg'].id,
                file_name=m['fn'], ext=m['ext'], size=m['size'],
                caption=m['caption'], gh_path=m['gh_path'] + '/' + m['fn'],
                status='partial', fail_stage='github', tg_ok=1, gh_ok=0,
                reason=f'download: {type(e).__name__}: {str(e)[:130]}',
                delivery=m['delivery'])
            stats['partial'] += 1
            log(f'   [DL FAIL] {m["fn"][:44]} {type(e).__name__}: {str(e)[:60]}')



async def upload_member(client, dest, unit, tid_box, m, sem, git,
                        pending_push, stats):
    """Protected source: re-upload path. Downloads first, then sends."""
    async with sem:
        tmp = os.path.join(BASE, '_tmp_upload',
                           f'{m["msg"].id}_{m["safe_fn"]}')
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        try:
            if m['size'] >= FAST_DL_MB * 1024 * 1024:
                with open(tmp, 'wb') as fh:
                    await fast_telethon.download_file(
                        client, m['msg'].document, fh)
            else:
                await client.download_media(m['msg'], file=tmp)
        except Exception as e:
            store.record(
                unit['group_id'], unit['topic_id'], m['msg'].id,
                file_name=m['fn'], ext=m['ext'], size=m['size'],
                caption=m['caption'], status='failed',
                fail_stage='telegram',
                reason=f'download: {type(e).__name__}: {str(e)[:130]}',
                delivery='none')
            stats['failed_tg'] += 1
            log(f'   [FAIL tg] {m["fn"][:44]} download {type(e).__name__}')
            return

        sent = None
        for attempt in (1, 2):
            try:
                sent = await client.send_file(
                    dest, tmp, reply_to=tid_box['tid'], force_document=True,
                    caption=(m['caption'] or '')[:1024])
                break
            except (errors.FloodWaitError, errors.SlowModeWaitError) as e:
                wait = min(e.seconds + 1, max(1, int(remaining())))
                if wait >= remaining() - 60 or attempt == 2:
                    store.record(
                        unit['group_id'], unit['topic_id'], m['msg'].id,
                        file_name=m['fn'], ext=m['ext'], size=m['size'],
                        caption=m['caption'], status='failed',
                        fail_stage='telegram',
                        reason=f'send floodwait {e.seconds}s', delivery='none')
                    stats['failed_tg'] += 1
                    log(f'   [FAIL tg] {m["fn"][:44]} floodwait {e.seconds}s')
                    return
                await asyncio.sleep(wait)
            except Exception as e:
                store.record(
                    unit['group_id'], unit['topic_id'], m['msg'].id,
                    file_name=m['fn'], ext=m['ext'], size=m['size'],
                    caption=m['caption'], status='failed',
                    fail_stage='telegram',
                    reason=f'{type(e).__name__}: {str(e)[:130]}',
                    delivery='none')
                stats['failed_tg'] += 1
                log(f'   [FAIL tg] {m["fn"][:44]} {type(e).__name__}')
                return

        # Delivered. Partial lands immediately (one small write) so a crash
        # before the batch flush can still never duplicate the upload.
        store.record(
            unit['group_id'], unit['topic_id'], m['msg'].id,
            dest_msg_id=getattr(sent, 'id', None), file_name=m['fn'],
            ext=m['ext'], size=m['size'], caption=m['caption'],
            gh_path=m['gh_path'] + '/' + m['fn'], status='partial',
            fail_stage='github', tg_ok=1, gh_ok=0, delivery='uploaded')
        git.add(m['gh_path'], tmp)          # moves the temp file into the tree
        if m['caption']:
            git.add(m['gh_dir'] + '/caption.txt', m['caption'].encode())
        pending_push.append((unit['group_id'], m['msg'].id))
        stats['uploaded'] += 1
        log(f'   [UP] {m["fn"][:52]} {m["size"] / 1024:.0f}KB')


async def maybe_flush(git, pending_push, stats, force=False):
    """Push staged files when the trigger fires, then flip their rows to done."""
    if not git.should_flush(force):
        return
    n = git.pending
    try:
        git.flush(f'archive: {n} file(s)')
    except Exception as e:
        log(f'   [GIT FAIL] push failed, {n} file(s) stay staged: '
            f'{str(e)[:140]}')
        return
    by_group = {}
    for gid, mid in pending_push:
        by_group.setdefault(gid, []).append(mid)
    for gid, ids in by_group.items():
        store.mark_pushed(gid, ids)
    stats['archived'] += len(pending_push)
    log(f'   [GIT] pushed {n} file(s) in one commit '
        f'({stats["archived"]} done this run)')
    pending_push.clear()



async def do_unit(client, dest, unit, git):
    """Walk one unit cursor-first: batch-forward, batch-download, batch-push."""
    mode = 'upload' if unit['noforwards'] else 'forward'
    label = (f"{unit['group_title'][:26]}"
             + (f" / {unit['title'][:24]}" if unit['topic_id'] else ''))
    log(f'\n=== UNIT {unit["id"]} {label} ({mode}) ===')

    src_ent = await client.get_entity(unit['group_id'])
    already, partial = store.done_msg_ids(
        unit['group_id'], unit['topic_id'], unit['cursor'] or 0)
    log(f'   ledger: {len(already)} settled, {len(partial)} awaiting commit')

    stats = {'archived': 0, 'forwarded': 0, 'uploaded': 0, 'ads': 0,
             'kind': 0, 'failed_tg': 0, 'partial': 0, 'recovered': 0,
             'bytes': 0, 'dry': 0}
    tid_box = {'tid': unit.get('dest_topic_id')}
    sem = asyncio.Semaphore(DL_CONCURRENCY)
    pending_push = []
    fwd_buf = []

    def gh_dir_for(fn, msg_id):
        parts = [layout.safe_name(unit['group_title'])]
        if unit['topic_id']:
            parts.append(layout.safe_name(unit['title']))
        parts.append(layout.file_folder(fn, msg_id))
        return '/'.join(parts)

    def classify(msg):
        """A member dict if archivable, else None (skips counted, no rows)."""
        ok, ext, why = layout.commit_decision(msg)
        fn = layout.doc_name(msg)
        caption = (msg.message or '').strip()
        if not ok:
            stats['kind'] += 1
            return None
        if adfilter.is_ad(caption, fn, has_study_doc=True):
            stats['ads'] += 1
            return None
        if layout.doc_size(msg) > MAX_FILE_MB * 1024 * 1024:
            stats['kind'] += 1
            return None
        safe_fn = layout.safe_name(fn, limit=120)
        return {
            'msg': msg, 'fn': fn, 'safe_fn': safe_fn, 'ext': ext,
            'size': layout.doc_size(msg), 'caption': caption,
            'gh_dir': gh_dir_for(fn, msg.id),
            'gh_path': gh_dir_for(fn, msg.id) + '/' + safe_fn,
            'delivery': 'uploaded' if mode == 'upload' else 'forwarded',
        }

    scanned = 0
    top_seen = unit['cursor'] or 0
    cap_hit = False
    last_hb = time.time()

    # Cursor pagination: walk ONLY what is newer than the cursor, oldest
    # first. Skipped kinds and ads get NO ledger row - the cursor is what
    # guarantees they are never revisited, which keeps D1 row-writes
    # proportional to delivered files, not to every message seen.
    kwargs = {'min_id': unit['cursor'] or 0, 'reverse': True}
    if unit['topic_id']:
        kwargs['reply_to'] = unit['topic_id']

    async for msg in client.iter_messages(src_ent, **kwargs):
        scanned += 1
        top_seen = max(top_seen, msg.id)
        if remaining() < 120:
            log('   [TIME] budget nearly gone, stopping this unit')
            break
        if scanned >= SCAN_LIMIT:
            cap_hit = True
            log(f'   [CAP] scan limit {SCAN_LIMIT} reached - unit continues '
                f'next run from cursor {top_seen}')
            break
        if time.time() - last_hb > 240:
            store.heartbeat(unit['id'], WORKER)
            last_hb = time.time()

        if msg.id in already:
            continue
        member = classify(msg)
        if member is None:
            continue

        if DRY:
            stats['dry'] += 1
            continue

        row = partial.get(msg.id)
        if row is not None:
            # Delivered in an earlier run - only the commit is owed. NEVER
            # re-forward: that is the duplicate this design exists to kill.
            await download_member(client, unit, member, sem, git,
                                  pending_push, stats, resumed=True)
            await maybe_flush(git, pending_push, stats)
            continue

        if mode == 'forward':
            fwd_buf.append(member)
            if len(fwd_buf) >= FWD_CHUNK:
                if tid_box['tid'] is None:
                    tid_box['tid'] = await ensure_dest_topic(
                        client, dest, unit)
                await forward_chunk(client, dest, unit, src_ent, fwd_buf,
                                    tid_box, stats)
                await asyncio.gather(*[
                    download_member(client, unit, m, sem, git,
                                    pending_push, stats) for m in fwd_buf])
                fwd_buf = []
                await maybe_flush(git, pending_push, stats)
        else:
            if tid_box['tid'] is None:
                tid_box['tid'] = await ensure_dest_topic(client, dest, unit)
            await upload_member(client, dest, unit, tid_box, member, sem, git,
                                pending_push, stats)
            await maybe_flush(git, pending_push, stats)

    if scanned >= SCAN_LIMIT:
        cap_hit = True
        log(f'   [CAP] scan limit {SCAN_LIMIT} reached - unit continues '
            f'next run from cursor {top_seen}')
    # Flush the trailing buffer BEFORE the cursor advances. If this raises
    # (floodwait beyond budget) the bump below is skipped, the cursor stays,
    # and the tail is re-walked next run - safe in both directions.
    if not DRY and fwd_buf:
        if tid_box['tid'] is None:
            tid_box['tid'] = await ensure_dest_topic(client, dest, unit)
        await forward_chunk(client, dest, unit, src_ent, fwd_buf, tid_box,
                            stats)
        await asyncio.gather(*[
            download_member(client, unit, m, sem, git, pending_push, stats)
            for m in fwd_buf])

    if not DRY:
        await maybe_flush(git, pending_push, stats, force=True)

    store.bump(unit['id'], archived=stats['archived'],
               forwarded=stats['forwarded'],
               ads=stats['ads'], kind=stats['kind'],
               failed_tg=stats['failed_tg'], failed_gh=0,
               partial=stats['partial'], bytes_=stats['bytes'],
               cursor=top_seen, top_msg=top_seen + (1 if cap_hit else 0))
    extra = f', dry {stats["dry"]}' if DRY else ''
    log(f'   scanned {scanned} | archived {stats["archived"]} '
        f'(fwd {stats["forwarded"]}, up {stats["uploaded"]}) | '
        f'ads {stats["ads"]} | other {stats["kind"]} | '
        f'tg_fail {stats["failed_tg"]} | pending commit {stats["partial"]}'
        f'{extra}')
    if stats['recovered']:
        log(f'   {stats["recovered"]} commit(s) recovered from a partial state')
    return stats



async def main():
    if not store.gate_open():
        log('[PAUSED] mock_paused=1 in meta - nothing to do')
        return

    git = None
    if not DRY:
        created, full = ghcommit.ensure_repo(private=True)
        git = ghbatch.GitArchive(WORKDIR, config.gh_token(),
                                 ghcommit.OWNER, ghcommit.REPO)
        git.ensure()
        log(f"[GIT] work tree ready at {WORKDIR}")
        log(f"[GITHUB] {full} ({'created' if created else 'exists'})")

    client = await get_client()
    me = await client.get_me()
    log(f'[ACCOUNT] {me.first_name} ({me.phone}) | worker={WORKER} '
        f'| budget {BUDGET_MIN:.0f} min')
    await warm_entity_cache(client)
    dest = await client.get_entity(sources.DEST)
    log(f'[DEST] {getattr(dest, "title", "?")} '
        f'(forum={getattr(dest, "forum", False)})')

    total = {'units': 0, 'archived': 0, 'forwarded': 0, 'uploaded': 0,
             'ads': 0, 'failed_tg': 0, 'partial': 0, 'recovered': 0,
             'dry': 0}
    # A unit that fails with the same exception over and over must not eat the
    # whole run: claim() orders unscanned units first, so a permanently broken
    # unit would be re-claimed after every release until the budget died. Three
    # consecutive failures quarantine it in D1 for 12 h instead.
    fails = {}
    # One read for the whole run instead of one per claim.
    queue = store.claimable_units() if ONLY_UNIT is None else []
    queue_refilled = False
    log(f'[QUEUE] {len(queue)} unit(s) claimable this run')
    while remaining() > 180:
        if MAX_UNITS and total['units'] >= MAX_UNITS:
            log(f'[STOP] reached --units {MAX_UNITS}')
            break
        try:
            if ONLY_UNIT is not None:
                unit = store.claim_specific(WORKER, ONLY_UNIT)
            else:
                # The queue is read ONCE per run (see store.claimable_units:
                # the old per-claim query cost ~2,976 rows_read and exhausted
                # D1's 5M/day read cap). take_lease() is a pure conditional
                # write, so two workers still cannot win the same unit.
                unit = None
                while queue:
                    cand = queue.pop(0)
                    if store.take_lease(cand['id'], WORKER):
                        unit = cand
                        break
                if unit is None and not queue_refilled:
                    queue = store.claimable_units()
                    queue_refilled = True
                    log(f'[QUEUE] refilled: {len(queue)} unit(s) claimable')
                    continue
        except store.QuotaExhausted as e:
            # Stop cleanly. Continuing without a ledger would re-forward and
            # re-commit everything on the next run, which is exactly the
            # duplicate class of bug this project keeps hitting.
            log(f'[QUOTA] D1 is refusing writes - stopping. {e}')
            log('[QUOTA] the cap resets at 00:00 UTC; nothing was lost.')
            break
        if not unit:
            log('[CLAIM] nothing claimable right now')
            break
        try:
            s = await do_unit(client, dest, unit, git)
            total['units'] += 1
            for k in ('archived', 'forwarded', 'uploaded', 'ads',
                      'failed_tg', 'partial', 'recovered', 'dry'):
                total[k] += s[k]
        except store.QuotaExhausted as e:
            log(f'[QUOTA] mid-unit: {e}')
            store.release(unit['id'], WORKER)
            break
        except Exception as e:
            n = fails.get(unit['id'], 0) + 1
            fails[unit['id']] = n
            log(f'[ERROR] unit {unit["id"]} failed (attempt {n}/3): '
                f'{type(e).__name__}: {str(e)[:160]}')
            if n >= 3:
                # Quarantine. Without this, claim() hands the same poisoned unit
                # straight back and the run burns its entire budget on it - the
                # entity-cache failure did exactly that for 40 minutes.
                until = store.block_unit(unit['id'], hours=12)
                log(f'[BLOCK] unit {unit["id"]} quarantined until {until} UTC '
                    f'after {n} consecutive failures')
                fails[unit['id']] = 0
        finally:
            try:
                store.release(unit['id'], WORKER)
            except Exception:
                pass

    log(f"\n=== RUN END: {total['units']} unit(s), "
        f"{total['archived']} archived (fwd {total['forwarded']}, "
        f"up {total['uploaded']}), {total['ads']} ads skipped, "
        f"tg-fail {total['failed_tg']}, pending-commit "
        f"{total['partial']}, recovered {total['recovered']}, "
        f"{(time.time() - START) / 60:.1f} min ===")
    if not DRY and git is not None and git.pending:
        try:
            n = git.flush('archive: final flush')
            log(f'[GIT] final flush pushed {n} file(s)')
        except Exception as e:
            log(f'[GIT] final flush FAILED: {str(e)[:140]} - rows stay '
                f'partial, recovered next run')
    await client.disconnect()


try:
    asyncio.run(main())
except store.QuotaExhausted as e:
    # A clean, NON-failing exit - same precedent as the course migration's
    # migrate.py: the run simply could not proceed, nothing is broken and no
    # data is at risk, so a red X would be misleading.
    #
    # This matters beyond cosmetics. D1 counts rows_read for an UPDATE's WHERE
    # clause too, so once the read cap is gone even `store.release()` can raise -
    # which is exactly how run 33942041409 failed AFTER its own quota handler had
    # already logged and stopped cleanly.
    print(f'[QUOTA] stopping cleanly: {str(e)[:160]}')
    print('[QUOTA] the cap resets at 00:00 UTC. Cursors and the ledger are '
          'intact; the next run resumes where this one stopped.')
    sys.exit(0)
