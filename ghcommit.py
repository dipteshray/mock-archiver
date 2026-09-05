"""GitHub committer: create the repo if needed, push files via the contents API.

No local git clone - the Contents API takes base64 content directly, which keeps
this runnable from a GitHub Action with nothing but a token.

Token comes from config.gh_token() (read from a gitignored file). It is never
printed, logged, or written into any committed file.
"""
import base64
import os
import time

import requests

import config

OWNER = os.getenv('GH_OWNER') or 'dipteshray'
REPO = os.getenv('GH_REPO') or 'mock-archive'
BRANCH = os.getenv('GH_BRANCH') or 'main'
API = 'https://api.github.com'


def _h():
    return {'Authorization': f'Bearer {config.gh_token()}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'mock-archiver'}


def ensure_repo(private=True):
    """Create the repo if it does not exist. Returns (created, full_name)."""
    r = requests.get(f'{API}/repos/{OWNER}/{REPO}', headers=_h(), timeout=40)
    if r.status_code == 200:
        return False, r.json()['full_name']
    if r.status_code != 404:
        raise RuntimeError(f'repo check failed HTTP {r.status_code}: {r.text[:200]}')
    r = requests.post(f'{API}/user/repos', headers=_h(), timeout=60, json={
        'name': REPO,
        'private': private,
        'description': 'Archived mock tests from Telegram (auto-committed)',
        'auto_init': True,
    })
    if r.status_code not in (200, 201):
        raise RuntimeError(f'repo create failed HTTP {r.status_code}: {r.text[:200]}')
    # auto_init commits a README asynchronously; give it a moment.
    time.sleep(2)
    return True, r.json()['full_name']


def get_sha(path):
    """Existing blob sha for `path`, or None. Needed to update a file."""
    r = requests.get(f'{API}/repos/{OWNER}/{REPO}/contents/{path}',
                     headers=_h(), params={'ref': BRANCH}, timeout=40)
    if r.status_code == 200:
        d = r.json()
        return d.get('sha') if isinstance(d, dict) else None
    return None


def exists(path):
    return get_sha(path) is not None


def put_file(path, data, message, tries=4):
    """Create or update one file. `data` may be bytes or str.

    Returns 'created' | 'updated' | 'identical'.
    """
    if isinstance(data, str):
        data = data.encode('utf-8')
    sha = get_sha(path)
    body = {'message': message, 'branch': BRANCH,
            'content': base64.b64encode(data).decode('ascii')}
    if sha:
        body['sha'] = sha
    for attempt in range(tries):
        r = requests.put(f'{API}/repos/{OWNER}/{REPO}/contents/{path}',
                         headers=_h(), json=body, timeout=120)
        if r.status_code in (200, 201):
            return 'updated' if sha else 'created'
        # 409/422 usually means a concurrent write moved the sha - refetch once.
        if r.status_code in (409, 422) and attempt < tries - 1:
            time.sleep(2 * (attempt + 1))
            new_sha = get_sha(path)
            if new_sha:
                body['sha'] = new_sha
            continue
        if r.status_code >= 500 and attempt < tries - 1:
            time.sleep(3 * (attempt + 1))
            continue
        raise RuntimeError(f'PUT {path} HTTP {r.status_code}: {r.text[:200]}')
    raise RuntimeError(f'PUT {path} failed after {tries} tries')
