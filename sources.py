"""The 14 source groups, exactly as supplied, plus the forward destination.

`ref` is whatever the user gave us - a @username, a numeric id, or a message
link for private groups we can only reach by id. resolve_sources.py turns every
one of these into a real entity and reports what it found; nothing else in the
project guesses.

`topics` and `forwardable` are the USER'S claims. They are starting hints only -
resolve_sources.py verifies both against Telegram and the verified value is what
the pipeline uses. Never trust this table over a live probe.
"""

# The new group everything gets forwarded into.
DEST = -1004421267340        # "Competitive exam mocks"

SOURCES = [
    {'n': 1, 'ref': '@ssc_exams_2026_cgl_chsl_steno',
     'name': 'SSC Exams 2026 CGL CHSL Steno',
     'topics': False, 'forwardable': True, 'note': 'public username'},

    {'n': 2, 'ref': -1003669983505,
     'name': 'CGL Tier-1 Sectional Mocks',
     'topics': False, 'forwardable': True, 'note': 'by id'},

    {'n': 3, 'ref': 'https://t.me/Yatri_Mock_Hub_CGL_Full_Tests',
     'name': 'Yatri Mock Hub CGL Full Tests',
     'topics': False, 'forwardable': True, 'note': 'public link'},

    {'n': 4, 'ref': -1004454233696,
     'name': 'Preparation Booster Mocks',
     'topics': False, 'forwardable': True, 'note': 'by id'},

    {'n': 5, 'ref': -1004334838420,
     'name': 'English PYQ Vault',
     'topics': False, 'forwardable': True, 'note': 'by id'},

    {'n': 6, 'ref': 'https://t.me/GK_Subjectwise_Pyqs_Tests',
     'name': 'GK Subjectwise PYQs Tests',
     'topics': False, 'forwardable': True, 'note': 'public link'},

    # Private, no join link. Only a post link was given, so the channel id is
    # derived from it: t.me/c/<internal>/<msg> -> -100<internal>.
    {'n': 7, 'ref': -1004466646097,
     'name': 'Private Mock Group 7',
     'topics': False, 'forwardable': False,
     'note': 'from post link t.me/c/4466646097/4293'},

    {'n': 8, 'ref': -1004337019501,
     'name': 'Private Mock Group 8 (topics)',
     'topics': True, 'forwardable': False,
     'note': 'from t.me/c/4337019501/32/1693; @Tgcloudtube_bot is a member'},

    {'n': 9, 'ref': 'https://t.me/EnglishMadhyamMock',
     'name': 'English Madhyam Mock',
     'topics': True, 'forwardable': False, 'note': 'public link, has topics'},

    {'n': 10, 'ref': -1004391368540,
     'name': 'Computer CGL T-2',
     'topics': False, 'forwardable': True, 'note': 'by id'},

    {'n': 11, 'ref': '@PiroMocks',
     'name': 'PiroMocks',
     'topics': False, 'forwardable': True, 'note': 'id -1003243595937',
     # User decision 2026-09-03: skip entirely. A 25-message sample held 16
     # text / 8 photo / 1 video and the only document was an .mp4 - no mocks.
     'skip': True},

    {'n': 12, 'ref': -1003852027504,
     'name': 'Private Mock Group 12 (topics)',
     'topics': True, 'forwardable': False,
     'note': 'from t.me/c/3852027504/2552/2617'},

    {'n': 13, 'ref': 'https://t.me/ThePunditsPaidMocks',
     'name': 'The Pundits Paid Mocks',
     'topics': True, 'forwardable': False, 'note': 'public link, has topics'},

    {'n': 14, 'ref': 'https://t.me/OliveBoard_CGL_Paid_Mock',
     'name': 'OliveBoard CGL Paid Mock',
     'topics': True, 'forwardable': False, 'note': 'public link, has topics'},
]

# Captions containing any of these (case-insensitive) are skipped when
# forwarding. Word-ish match so "#address" is not treated as "#ad".
AD_TAGS = ('#ad', '#ads')

# Only these get committed to GitHub. Everything else - videos, plain text,
# link-only posts, stickers, audio - is deliberately excluded.
COMMIT_EXTS = (
    '.html', '.htm',
    '.pdf',
    '.doc', '.docx',
    '.xls', '.xlsx',
    '.ppt', '.pptx',
    '.txt', '.csv', '.json',
    '.zip', '.rar', '.7z',
)

# Never commit these even if they arrive as a document.
SKIP_EXTS = (
    '.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.3gp', '.flv',
    '.mp3', '.m4a', '.ogg', '.oga', '.opus', '.wav', '.aac',
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tgs', '.webm',
)
