"""Offline proof that the read-reduction changes preserve every guarantee.

    python test_read_reduction.py

No network, no D1, no Telegram: store's query functions are stubbed so the
LOGIC can be asserted. Checks the three properties the user asked about:
  1. no duplicate forward is possible for an already-delivered file
  2. a partial (Telegram-done, GitHub-owed) is ALWAYS found, at any cursor
  3. the above-cursor window is still checked, so a crashed run cannot re-forward
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import store                                            # noqa: E402

# --- fake ledger ------------------------------------------------------------
LEDGER = [
    # settled long ago, far below the cursor
    dict(msg_id=100, status='done', tg_ok=1, gh_ok=1, dest_msg_id=1,
         gh_path='a', file_name='old.html', ext='.html', size=1, caption='',
         delivery='forwarded'),
    # settled, just below the cursor
    dict(msg_id=499, status='done', tg_ok=1, gh_ok=1, dest_msg_id=2,
         gh_path='b', file_name='recent.html', ext='.html', size=1, caption='',
         delivery='forwarded'),
    # forwarded but the run died before the cursor advanced -> ABOVE cursor
    dict(msg_id=505, status='done', tg_ok=1, gh_ok=1, dest_msg_id=3,
         gh_path='c', file_name='window.html', ext='.html', size=1, caption='',
         delivery='forwarded'),
    # in Telegram, commit owed - far below the cursor (must STILL be found)
    dict(msg_id=250, status='partial', tg_ok=1, gh_ok=0, dest_msg_id=4,
         gh_path='d', file_name='owed.html', ext='.html', size=1, caption='',
         delivery='forwarded'),
]
CURSOR = 500
captured = {}


def fake_q(sql, params=None):
    """Emulate BOTH real queries against the fake ledger.

    done_msg_ids now issues two indexed queries instead of one OR'd scan, so the
    stub dispatches on which one it was handed.
    """
    captured.setdefault('sqls', []).append(sql)
    if 'msg_id > ?' in sql:
        gid, cur, tid = params
        out = [r for r in LEDGER if r['msg_id'] > cur]
        captured['above'] = len(out)
        return out
    if "status = 'partial'" in sql:
        out = [r for r in LEDGER if r['status'] == 'partial']
        captured['owed'] = len(out)
        return out
    raise AssertionError('unexpected query: ' + sql)


store.q = fake_q
done, partial = store.done_msg_ids(4466646097, 0, CURSOR)

checks = []
checks.append(('above-cursor settled file IS seen (no re-forward)',
               505 in done))
checks.append(('partial below cursor IS still found (commit retried)',
               250 in partial))
checks.append(('partial is NOT in done (would skip the commit)',
               250 not in done))
checks.append(('below-cursor settled rows are NOT read (cost saved)',
               100 not in done and 499 not in done))
checks.append(('two indexed queries, not one OR scan',
               len(captured['sqls']) == 2))
checks.append(('query 1 is the cursor range scan',
               'msg_id > ?' in captured['sqls'][0]
               and 'OR' not in captured['sqls'][0]))
checks.append(('query 2 hits the status index for partials',
               "status = 'partial'" in captured['sqls'][1]
               and 'msg_id >' not in captured['sqls'][1]))
checks.append(('rows read is a small window, not the whole unit',
               captured['above'] + captured['owed'] < len(LEDGER)))

print('done set    :', sorted(done))
print('partial keys:', sorted(partial))
print()
ok = True
for label, passed in checks:
    print(f'  [{"PASS" if passed else "FAIL"}] {label}')
    ok = ok and passed

# --- claim path: the lease is still a conditional write --------------------
src = open(os.path.join(BASE, 'store.py'), encoding='utf-8').read()
lease_guard = ('WHERE id = ? AND (claimed_at IS NULL OR claimed_at < ?)'
               in src)
print(f'\n  [{"PASS" if lease_guard else "FAIL"}] take_lease keeps the '
      f'staleness guard (two workers cannot win one unit)')
ok = ok and lease_guard

print('\nALL CHECKS PASSED' if ok else '\nSOME CHECKS FAILED')
sys.exit(0 if ok else 1)
