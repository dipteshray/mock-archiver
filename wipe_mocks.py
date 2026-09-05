"""Wipe every mock artefact and reset the archive to a clean start.

    python wipe_mocks.py            # show exactly what would go
    python wipe_mocks.py --go       # do it

Three targets, all mock-only:

  TELEGRAM  every topic in DEST except General, plus any loose message
  GITHUB    the whole tree of the archive repo, back to README.md
  D1        every `mock_files` row; `mock_topics` counters/cursors reset to 0

The 14 groups and 93 topics in D1 are KEPT - they are the resolved structure
(ids, titles, forum flags, protection flags) that took a full pass over live
Telegram to build. Deleting them would mean re-resolving for no benefit; zeroing
the cursors already makes the next run start from the beginning.

Source groups are never touched. They are read-only in this project, always.
"""
import asyncio
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import requests                                  # noqa: E402
from telethon import TelegramClient              # noqa: E402
# Both live in functions.messages in telethon 1.44; importing
# DeleteTopicHistoryRequest from functions.channels raises ImportError, and the
# parameter is `peer=`, not `channel=`.
from telethon.tl.functions.messages import (     # noqa: E402
    DeleteTopicHistoryRequest, GetForumTopicsRequest)

import config                                     # noqa: E402
import ghcommit                                   # noqa: E402
import mockdb                                     # noqa: E402
import sources                                    # noqa: E402

GO = '--go' in sys.argv


async def wipe_telegram():
    print('=== TELEGRAM ===')
    client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)
    await client.connect()
    dest = await client.get_entity(sources.DEST)
    print(f'  group: {dest.title}')

    res = await client(GetForumTopicsRequest(
        peer=dest, offset_date=None, offset_id=0, offset_topic=0, limit=100))
    # Topic 1 is General - it cannot be deleted and holds nothing of ours.
    topics = [t for t in res.topics if t.id != 1]
    print(f'  {len(res.topics)} topic(s) present, {len(topics)} to delete')
    for t in topics:
        print(f"    {'DELETE' if GO else 'would delete'} id={t.id} "
              f"{(t.title or '')[:56]}")
        if GO:
            # Removes the messages AND the topic itself.
            await client(DeleteTopicHistoryRequest(peer=dest, top_msg_id=t.id))

    if GO:
        loose = [m.id async for m in client.iter_messages(dest, limit=300)
                 if m.id != 1]
        if loose:
            for i in range(0, len(loose), 100):
                await client.delete_messages(dest, loose[i:i + 100])
            print(f'    deleted {len(loose)} loose message(s) from General')

    after = await client(GetForumTopicsRequest(
        peer=dest, offset_date=None, offset_id=0, offset_topic=0, limit=100))
    print(f'  topics remaining: {len(after.topics)}')
    await client.disconnect()


def wipe_github():
    print('\n=== GITHUB ===')
    H = {'Authorization': f'Bearer {config.gh_token()}',
         'Accept': 'application/vnd.github+json',
         'User-Agent': 'mock-archiver'}
    base = f'https://api.github.com/repos/{ghcommit.OWNER}/{ghcommit.REPO}'
    r = requests.get(f'{base}/git/trees/{ghcommit.BRANCH}', headers=H,
                     params={'recursive': '1'}, timeout=60)
    if r.status_code == 404:
        print('  repo does not exist - nothing to wipe')
        return
    tree = r.json().get('tree', [])
    blobs = [x for x in tree if x['type'] == 'blob' and x['path'] != 'README.md']
    print(f'  {len(blobs)} file(s) to delete (README.md kept)')
    for b in blobs[:5]:
        print(f"    {'DELETE' if GO else 'would delete'} {b['path'][:92]}")
    if len(blobs) > 5:
        print(f'    ... and {len(blobs) - 5} more')
    if not GO or not blobs:
        return

    # One commit replacing the whole tree: atomic, and far cheaper than N
    # DELETE calls. It cannot leave the repo half-cleaned.
    head = requests.get(f'{base}/git/ref/heads/{ghcommit.BRANCH}',
                        headers=H, timeout=40).json()
    parent = head['object']['sha']
    readme = next((x for x in tree if x['path'] == 'README.md'), None)
    entry = ({'path': 'README.md', 'mode': readme['mode'], 'type': 'blob',
              'sha': readme['sha']} if readme else
             {'path': 'README.md', 'mode': '100644', 'type': 'blob',
              'content': '# mock-archive\n'})
    tr = requests.post(f'{base}/git/trees', headers=H, timeout=60,
                       json={'tree': [entry]})
    if tr.status_code not in (200, 201):
        raise RuntimeError(f'tree create failed {tr.status_code}: {tr.text[:200]}')
    cr = requests.post(f'{base}/git/commits', headers=H, timeout=60, json={
        'message': 'wipe: clear the archive for a clean rerun',
        'tree': tr.json()['sha'], 'parents': [parent]})
    if cr.status_code not in (200, 201):
        raise RuntimeError(f'commit failed {cr.status_code}: {cr.text[:200]}')
    ur = requests.patch(f'{base}/git/refs/heads/{ghcommit.BRANCH}', headers=H,
                        timeout=60,
                        json={'sha': cr.json()['sha'], 'force': True})
    print(f'  ref update HTTP {ur.status_code}')
    left = requests.get(f'{base}/git/trees/{ghcommit.BRANCH}', headers=H,
                        params={'recursive': '1'}, timeout=60).json()
    print('  files now in repo: '
          f"{len([x for x in left.get('tree', []) if x['type'] == 'blob'])}")


def wipe_d1():
    print('\n=== CLOUDFLARE D1 ===')
    f = mockdb.scalar('SELECT COUNT(*) FROM mock_files')
    arch = mockdb.scalar('SELECT COALESCE(SUM(archived),0) FROM mock_topics')
    dt = mockdb.scalar('SELECT COUNT(*) FROM mock_topics '
                       'WHERE dest_topic_id IS NOT NULL')
    print(f'  mock_files rows        : {f}  -> 0')
    print(f'  archived counter total : {arch}  -> 0')
    print(f'  dest_topic_id set on   : {dt} topic(s)  -> cleared')
    print(f'  mock_groups            : '
          f"{mockdb.scalar('SELECT COUNT(*) FROM mock_groups')}  -> KEPT")
    print(f'  mock_topics rows       : '
          f"{mockdb.scalar('SELECT COUNT(*) FROM mock_topics')}  -> KEPT, reset")
    if not GO:
        return

    mockdb.d1('DELETE FROM mock_files')
    # Every counter, cursor, claim and destination id back to a clean state. The
    # rows themselves stay: they hold the resolved Telegram structure.
    mockdb.d1('UPDATE mock_topics SET cursor = 0, archived = 0, forwarded = 0, '
              'skipped_ads = 0, skipped_kind = 0, failed = 0, failed_tg = 0, '
              'failed_gh = 0, partial = 0, bytes = 0, dest_topic_id = NULL, '
              'claimed_at = NULL, claimed_by = NULL, blocked_until = NULL, '
              'last_scan_at = NULL')
    print('  wiped and reset')
    print(f"  verify: files={mockdb.scalar('SELECT COUNT(*) FROM mock_files')} "
          f"archived={mockdb.scalar('SELECT COALESCE(SUM(archived),0) FROM mock_topics')} "
          f"groups={mockdb.scalar('SELECT COUNT(*) FROM mock_groups')} "
          f"topics={mockdb.scalar('SELECT COUNT(*) FROM mock_topics')}")


def wipe_local():
    print('\n=== LOCAL ===')
    dl = os.path.join(BASE, 'downloads')
    n = len(os.listdir(dl)) if os.path.isdir(dl) else 0
    print(f'  downloads/: {n} file(s)')
    if GO and n:
        for x in os.listdir(dl):
            try:
                os.remove(os.path.join(dl, x))
            except OSError:
                pass
        print('  cleared')


async def main():
    if not GO:
        print('DRY RUN - nothing will be deleted. Pass --go to execute.\n')
    await wipe_telegram()
    wipe_github()
    wipe_d1()
    wipe_local()
    print('\ndone')


asyncio.run(main())
