"""Chain the next archive run if work remains. Runs as the LAST workflow step.

    python chain_next.py          # dispatch next run, or explain why not

Why: the 3-hourly schedule leaves the archive idle for up to 3 h after every
run finishes, even with 70 units still queued. This makes runs self-chaining:
finish -> check D1 -> if any unit still has work, dispatch the next run at once.

It stops dispatching when ANY of these hold:
  - nothing remains (archive complete until new messages arrive)
  - a 30-minute cooldown since the last chain - protects against a tight loop
    when everything that remains is quarantined (blocked units are excluded from
    "remaining", but the cooldown is the second line of defence)
  - the pause gate (`mock_paused`) is set

`gh workflow run` needs the Actions token, which the workflow supplies as
GH_TOKEN (secrets.GITHUB_TOKEN) with `actions: write` permission.
"""
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import mockdb                                     # noqa: E402
import store                                      # noqa: E402

COOLDOWN_MS = 30 * 60 * 1000


def remaining_units():
    """Units that still have work: never scanned, or cursor behind top_msg.

    Quarantined units are excluded - a unit that failed 3 times must not keep
    triggering runs for the next 12 h.
    """
    now = int(time.time() * 1000)
    rows = mockdb.d1(
        'SELECT COUNT(*) n FROM mock_topics t '
        'JOIN mock_groups g ON g.group_id = t.group_id '
        'WHERE g.skip = 0 '
        '  AND COALESCE(t.blocked_until, 0) < ? '
        '  AND (t.last_scan_at IS NULL '
        '       OR COALESCE(t.cursor, 0) < COALESCE(t.top_msg, 0))', [now])
    return rows[0]['n']


def main():
    # A monitoring step must never fail a run whose work succeeded.
    # Both mock runs on 2026-09-05 went red purely because this step
    # raised D1Error after the archiver had already exited cleanly on
    # the read-quota cap.
    try:
        _main()
    except (store.QuotaExhausted, mockdb.D1Error) as e:
        print(f'[CHAIN] D1 quota exhausted - not chaining. {str(e)[:120]}')
        print('[CHAIN] the cap resets at 00:00 UTC; the cron will resume.')
    except Exception as e:
        print(f'[CHAIN] skipped: {type(e).__name__}: {str(e)[:140]}')


def _main():
    if not store.gate_open():
        print('[CHAIN] gate is paused - not chaining')
        return

    left = remaining_units()
    print(f'[CHAIN] units with work remaining: {left}')
    if left == 0:
        print('[CHAIN] archive is up to date - no next run needed')
        return

    # Cooldown, stored in D1 so it holds across runners.
    last = mockdb.scalar(
        "SELECT value FROM mock_meta WHERE key = 'last_chain_at'")
    if last:
        age = time.time() * 1000 - int(last)
        if age < COOLDOWN_MS:
            print(f'[CHAIN] last chain {age / 60000:.0f} min ago - cooldown, '
                  f'skipping (next schedule run covers it)')
            return

    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        print('[CHAIN] no GH_TOKEN in env - cannot dispatch')
        sys.exit(1)

    env = os.environ.copy()
    env['GH_TOKEN'] = token
    r = subprocess.run(
        ['gh', 'workflow', 'run', 'archive.yml',
         '--repo', 'dipteshray/mock-archiver', '--ref', 'main'],
        capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print(f'[CHAIN] dispatch failed: {(r.stderr or r.stdout)[:200]}')
        sys.exit(1)
    print('[CHAIN] dispatched the next run - it will start once this one '
          'releases the concurrency slot')

    mockdb.d1("INSERT INTO mock_meta (key, value) VALUES ('last_chain_at', ?) "
              'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
              [str(int(time.time() * 1000))])


if __name__ == '__main__':
    main()
