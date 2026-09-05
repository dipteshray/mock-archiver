"""Schema v3: trim mock_files indexes (each index entry is a D1 row-write).

    python migrate_schema_v3.py --go

The free tier counts 100,000 row-writes per day PER ACCOUNT, and Cloudflare
counts index entries as row writes. The old table had 4 secondary indexes on
mock_files, so every archived file cost ~6 row writes (row + unique + 4
indexes). At the new speed that would blow the daily cap within hours. This
drops every index except the status lookup, cutting the cost to ~3 per file.
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import mockdb                                    # noqa: E402

DROP = ('idx_mock_files_group', 'idx_mock_files_stage', 'idx_mock_files_ts',
        'idx_mock_files_name')
KEEP = ('idx_mock_files_status', 'idx_mock_topics_claim')

GO = '--go' in sys.argv

if GO:
    for ix in DROP:
        mockdb.d1(f'DROP INDEX IF EXISTS {ix}')
        print(f'dropped {ix}')
else:
    print('DRY RUN - would drop:', ', '.join(DROP))

left = [r['name'] for r in mockdb.d1(
    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='mock_files' "
    'AND name NOT LIKE "sqlite_auto%"')]
print('remaining indexes:', left)
missing = [i for i in KEEP if i not in left]
if missing:
    if GO:
        for ix in missing:
            mockdb.d1(f'CREATE INDEX IF NOT EXISTS {ix} '
                      f'ON mock_files({"(status)" if ix.endswith("status") else "(claimed_at, cursor, top_msg)"})')
        print('re-created missing keepers')
    else:
        print('missing keepers:', missing)
