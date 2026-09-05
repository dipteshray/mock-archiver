"""D1 client for the mock archiver's OWN database, on its OWN Cloudflare account.

Why this exists instead of reusing `gori died 2/cf_token.py`:

1. **Separate account.** The migration fleet burns 18k+ row writes a day and hit
   the 100,000/day free-tier cap, which took the archiver down with it. The mock
   system now lives on account 58bd8ff... with its own quota, so neither project
   can starve the other.

2. **Native parameter types.** `cf_token.py` does
   `params = [str(p) for p in params]`. That is a real bug for this schema: in
   SQLite a TEXT value always compares GREATER than any INTEGER, so
   `claimed_at < '1757...'` is true for *every* row and the claim lease stops
   working - two workers would take the same unit and double-forward it. Here
   ints stay ints.

Credentials come from the environment, with the archiver's account as default.
The token is a scoped D1 token; never print it.
"""
import json
import os
import urllib.error
import urllib.request

ACC = os.getenv('MOCK_CF_ACCOUNT') or '58bd8ffab81a9612e503ca4426ef702b'
DB = os.getenv('MOCK_CF_DB') or 'afaf640d-a0a0-4147-9ecb-201a4dd3f6e8'


def _token():
    """Token from the environment, falling back to a gitignored local file.

    CI passes MOCK_CF_TOKEN as a secret; locally the file is more convenient than
    re-exporting the variable in every shell. Never printed either way.
    """
    t = os.getenv('MOCK_CF_TOKEN')
    if t:
        return t.strip()
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     '_mock_cf_token.txt')
    if os.path.exists(p):
        return open(p, encoding='utf-8').read().strip()
    return ''


TOKEN = _token()

URL = (f'https://api.cloudflare.com/client/v4/accounts/{ACC}'
       f'/d1/database/{DB}/query')


class D1Error(RuntimeError):
    pass


def _post(sql, params):
    if not TOKEN:
        raise D1Error('MOCK_CF_TOKEN is not set in the environment')
    body = {'sql': sql}
    if params is not None:
        # Keep ints as ints; only coerce what JSON cannot carry.
        body['params'] = [
            p if isinstance(p, (int, float, str)) or p is None else str(p)
            for p in params]
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode('utf-8'),
        headers={'Authorization': f'Bearer {TOKEN}',
                 'Content-Type': 'application/json'},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise D1Error(f'HTTP {e.code}: {e.read().decode("utf-8")[:300]}') from e
    if not d.get('success'):
        raise D1Error(f'D1 error: {d.get("errors")}')
    return d['result'][0]


def d1(sql, params=None):
    return _post(sql, params).get('results', [])


def meta(sql, params=None):
    res = _post(sql, params)
    return res.get('results', []), res.get('meta', {})


def one(sql, params=None):
    rows = d1(sql, params)
    return rows[0] if rows else None


def scalar(sql, params=None):
    r = one(sql, params)
    return None if not r else list(r.values())[0]
