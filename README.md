# mock-archiver

Archives mock-test papers from Telegram source groups into three places at once:

- **Telegram** - forwarded (hidden author) or re-uploaded into one destination
  forum group, one topic per source topic
- **GitHub** - `dipteshray/mock-archive` (private), `<group>/[<topic>/]<file
  title [msg_id]>/` plus `caption.txt`
- **Cloudflare D1** - the ledger that makes every re-run idempotent

## How it runs

GitHub Actions (`archive.yml`) starts a run every 3 hours. Each run claims units
of work from D1 (a whole channel, or one topic of a forum), walks its messages
newest-first, skips anything the ledger has settled, and resumes `partial` rows
by committing only - never re-forwarding.

Failures are recorded per stage: `failed_tg` (delivery refused at source) and
`failed_gh` (commit rejected while the file is already in the group). The
frontend shows them apart because they need different fixes.

State lives in D1, so this checkout is stateless. Secrets carry everything:
`MOCK_SESSION` (Telegram), `MOCK_CF_TOKEN/ACCOUNT/DB` (the archive's own
Cloudflare account - deliberately NOT the migration's, whose row-write quota the
course fleet consumes), and `ARCHIVE_GH_*` for the archive repo.
