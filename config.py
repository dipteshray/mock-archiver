"""Shared config for the mock-archiver worker.

This is a SEPARATE subproject from `gori died 2`. It uses its own Telegram
account (+919051905057) and its own session file, so it can never collide with
the five migration-fleet sessions - connecting one of those from a second place
burns its auth key (AuthKeyDuplicatedError).

Nothing secret is hardcoded here except the API id/hash, which is the same
developer app already used across this workspace. The session string and the
GitHub token live in files that .gitignore excludes.
"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))

# Same Telegram developer app used by the rest of the workspace.
API_ID = int(os.getenv('TG_API_ID') or 1982411)
API_HASH = os.getenv('TG_API_HASH') or '6b76fb0dae71d1ae99daefa714a1cf48'

# The archiver account. One account does all of this work, per the brief.
PHONE = os.getenv('ARCHIVER_PHONE') or '+919051905057'

# Telethon file-session (local only). Matches .clineignore's **/*.session rule.
SESSION_PATH = os.path.join(BASE, 'archiver.session')
SESSION_NAME = os.path.join(BASE, 'archiver')

# StringSession export, for later use in GitHub Actions secrets.
SESSION_STRING_FILE = os.path.join(BASE, '_session_string_archiver.txt')

# phone + phone_code_hash between login step 1 and step 2.
LOGIN_HASH_FILE = os.path.join(BASE, '_login_hash_archiver.txt')

# GitHub classic token, read from disk - never from source.
GH_TOKEN_FILE = os.path.join(BASE, '_gh_token.txt')


def gh_token():
    """Read the GitHub token from disk or the environment. Never log the value.

    GH_TOKEN_VALUE is how CI supplies it (a file cannot be committed). The local
    file stays the convenient path when running by hand.
    """
    env = os.getenv('GH_TOKEN_VALUE')
    if env and env.strip():
        return env.strip()
    if not os.path.exists(GH_TOKEN_FILE):
        raise RuntimeError(
            f'missing {os.path.basename(GH_TOKEN_FILE)} - write the classic '
            f'token into that file (it is gitignored), or set GH_TOKEN_VALUE')
    t = open(GH_TOKEN_FILE, encoding='utf-8').read().strip()
    if not t:
        raise RuntimeError(f'{os.path.basename(GH_TOKEN_FILE)} is empty')
    return t

