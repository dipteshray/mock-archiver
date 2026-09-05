"""Add per-stage failure tracking to the mock schema. Idempotent.

    python migrate_schema_v2.py            # show what is missing
    python migrate_schema_v2.py --go       # add it

Why: `failed` was a single number, so "Telegram refused the forward" and "GitHub
refused the commit" were indistinguishable. They need completely different
responses - a Telegram failure usually means the source is protected or the file
is too big, a GitHub failure usually means a token/size/rate problem - so they are
counted and displayed separately now.

The important part is not the counters, it is `fail_stage` + the `partial` status.
A message is delivered to Telegram BEFORE it is committed to GitHub. If GitHub
then fails and we record nothing, the next run re-forwards the same file and the
destination group gets a duplicate. Recording `partial` lets the retry skip the
Telegram step and only redo the commit.
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import mockdb                                    # noqa: E402

GO = '--go' in sys.argv

WANT = {
    'mock_topics': [
        # Split of the old single `failed` counter.
        ('failed_tg', 'INTEGER DEFAULT 0'),
        ('failed_gh', 'INTEGER DEFAULT 0'),
        # Delivered to Telegram but not yet committed to GitHub.
        ('partial', 'INTEGER DEFAULT 0'),
    ],
    'mock_files': [
        # '' | 'telegram' | 'github' - which side refused.
        ('fail_stage', 'TEXT'),
        # Kept so a retry knows the Telegram half is already done.
        ('tg_ok', 'INTEGER DEFAULT 0'),
        ('gh_ok', 'INTEGER DEFAULT 0'),
    ],
}


def columns(table):
    return {r['name'] for r in mockdb.d1(f'PRAGMA table_info({table})')}


todo = []
for table, cols in WANT.items():
    have = columns(table)
    for name, decl in cols:
        if name in have:
            print(f'  {table}.{name:12} already present')
        else:
            todo.append((table, name, decl))
            print(f'  {table}.{name:12} MISSING -> {decl}')

if not todo:
    print('\nnothing to do - schema already at v2')
    sys.exit(0)

if not GO:
    print(f'\n{len(todo)} column(s) to add. DRY RUN - pass --go')
    sys.exit(0)

for table, name, decl in todo:
    mockdb.d1(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')
    print(f'  added {table}.{name}')

# An index on fail_stage so the status page can list failures without scanning.
mockdb.d1('CREATE INDEX IF NOT EXISTS idx_mock_files_stage '
          'ON mock_files(fail_stage)')
print('  index idx_mock_files_stage ensured')

print('\nverify:')
for table in WANT:
    print(f'  {table}: {sorted(columns(table))}')
