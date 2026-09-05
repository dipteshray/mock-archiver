"""Shared helpers: message classification, safe path/name building.

Kept separate from the pipeline scripts so the folder layout is defined in ONE
place and the test harness exercises exactly what production uses.
"""
import os
import re

import sources

# Windows-illegal characters plus control chars.
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Runs of whitespace/dots that break paths or look ugly.
_DOTS = re.compile(r'\.+$')


def safe_name(name, fallback='untitled', limit=90):
    """A filesystem- and git-safe folder/file name.

    Deliberately NOT slugified to lowercase-ascii: these titles are Unicode
    (bold/italic maths letters, Devanagari) and mangling them loses the meaning.
    Only genuinely illegal characters are removed.
    """
    s = (name or '').strip()
    s = _BAD.sub('_', s)
    s = s.replace('\n', ' ').replace('\r', ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    s = _DOTS.sub('', s).strip()
    if not s:
        s = fallback
    # Windows reserves these regardless of extension.
    if s.upper() in {'CON', 'PRN', 'AUX', 'NUL'} or \
            re.match(r'^(COM|LPT)[1-9]$', s.upper()):
        s = '_' + s
    return s[:limit].strip() or fallback


def doc_name(msg):
    d = getattr(msg, 'document', None)
    if not d:
        return None
    for at in d.attributes:
        n = getattr(at, 'file_name', None)
        if n:
            return n
    return None


def doc_size(msg):
    d = getattr(msg, 'document', None)
    return (getattr(d, 'size', 0) or 0) if d else 0


def kind_of(msg):
    if msg.photo:
        return 'photo'
    if msg.video or msg.video_note or msg.gif:
        return 'video'
    if msg.audio or msg.voice:
        return 'audio'
    if msg.sticker:
        return 'sticker'
    if msg.document:
        return 'document'
    if msg.poll:
        return 'poll'
    if msg.message:
        return 'text'
    return 'other'


def commit_decision(msg):
    """(should_commit, extension, reason)

    Only real documents with a study extension are committed. Videos, audio,
    photos, stickers, polls and plain text never are - per the brief.
    """
    fn = doc_name(msg)
    if not fn:
        return False, None, f'no document ({kind_of(msg)})'
    ext = os.path.splitext(fn)[1].lower()
    if ext in sources.SKIP_EXTS:
        return False, ext, f'skip-ext {ext}'
    if ext not in sources.COMMIT_EXTS:
        return False, ext, f'not-study-ext {ext}'
    return True, ext, 'ok'


def file_folder(file_name, msg_id):
    """Folder name for one file: its exact title, with the message id appended.

    The id is ALWAYS appended, not just on collision. Two reasons:
      - group 8 reuses `mocks_wallah_49f6bfbd.html` across 8 different topics,
        and group 14 has five topics whose long names truncate identically;
      - an incremental daily run must be able to tell "already archived" from
        "same title, new file" without reading the whole tree.
    """
    stem = os.path.splitext(file_name or '')[0]
    return safe_name(f'{stem} [{msg_id}]', fallback=f'msg-{msg_id}')


def dest_topic_title(group_title, topic_title=None):
    """`topic - group` for the forward destination.

    Several sources have a `General` (and `ADMIN`) topic, so the group name is
    required to keep them apart in the single destination group.
    """
    g = safe_name(group_title, limit=60)
    if not topic_title:
        return g[:100]
    t = safe_name(topic_title, limit=60)
    return f'{t} - {g}'[:100]
