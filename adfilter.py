"""Ad detection. Built from a live audit of 7,521 study captions vs 1,312
non-study captions (build_ad_filter.py, 2026-09-03).

The governing rule: NEVER lose main content. Every keyword here was verified to
appear in ZERO captions belonging to a real study document. Three plausible words
were rejected because they DO appear on study files:

    profit   - 46 study captions ("profit and loss" maths questions)
    income   -  4 study captions ("income tax", "national income")
    withdraw -  1 study caption

That is why the filter is keyword-audited rather than hand-written.

Defence is layered, because no single signal is reliable:
  1. extension     - only COMMIT_EXTS are ever archived (kills every ad video)
  2. ad keywords   - audited, non-study-safe phrases
  3. ad channels   - usernames/domains seen only in ads
  4. UUID filename - ad media arrives as 11b74917-....mp4; study files never do
  5. link-only     - a caption that is nothing but links, with no document
"""
import os
import re

# Audited safe. Substring match on a lowercased caption.
AD_KEYWORDS = (
    '#ad', '#ads', '#promo', '#sponsored', '#paid',
    'trading', 'join fast', 'giveaway', 'dm me',
    'buy socials', 'verify apps', 'socials and numbers',
    'usdt', 'earning usdt', 'start earning', 'referral', 'investment',
    'ai agent', 'paid promotion', 'advertisement',
    'crypto', 'binance', 'betting', 'casino', 'lottery',
    'colour prediction', 'work from home', 'otp service',
)

# Handles/domains that only ever appeared in ad posts during the scan.
AD_SOURCES = (
    'rickysocials', 'insideads_bot', 'apps-bossxcode',
    'link.testbook.com', 'testbook.com/invite',
)

_UUID = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
_URLISH = re.compile(r'https?://\S+|t\.me/\S+|@[A-Za-z0-9_]{4,}')


def uuid_filename(file_name):
    """Ad media is uploaded with a bare UUID name; study files are titled."""
    if not file_name:
        return False
    return bool(_UUID.match(os.path.splitext(file_name)[0]))


def link_only(caption):
    """Caption is nothing but links/handles - no human text left over."""
    cap = (caption or '').strip()
    if not cap:
        return False
    stripped = _URLISH.sub('', cap)
    stripped = re.sub(r'[\s\W_]+', '', stripped)
    return len(stripped) < 8


def is_ad(caption, file_name=None, has_study_doc=False):
    """True if this message should be treated as an ad.

    `has_study_doc` is the escape hatch: when a message carries a genuine study
    document, only an explicit #ad tag can classify it as an ad. Promo footers
    are extremely common on legitimate mock posts, so keyword matching alone
    would throw away real files.
    """
    cap = (caption or '').lower()

    explicit = any(t in cap for t in ('#ad', '#ads', '#promo', '#sponsored'))
    if has_study_doc:
        return explicit

    if explicit:
        return True
    if uuid_filename(file_name):
        return True
    for w in AD_KEYWORDS:
        if w in cap:
            return True
    for s in AD_SOURCES:
        if s in cap:
            return True
    if link_only(cap):
        return True
    return False


def reason(caption, file_name=None, has_study_doc=False):
    """Why is_ad() fired - for logging, never for control flow."""
    cap = (caption or '').lower()
    if any(t in cap for t in ('#ad', '#ads', '#promo', '#sponsored')):
        return 'explicit-tag'
    if has_study_doc:
        return ''
    if uuid_filename(file_name):
        return 'uuid-filename'
    for w in AD_KEYWORDS:
        if w in cap:
            return f'keyword:{w}'
    for s in AD_SOURCES:
        if s in cap:
            return f'ad-source:{s}'
    if link_only(cap):
        return 'link-only'
    return ''
