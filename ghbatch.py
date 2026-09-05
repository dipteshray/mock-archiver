"""Batched GitHub commits for the archive repo.

Why not the Contents API: it costs 5 rate-limit points per PUT and we make 2
per file (file + caption.txt) = 10 points/file. On the Actions GITHUB_TOKEN
(1,000 points/hour) that is a hard ceiling of ~100 files/hour - the exact
bottleneck the fleet hit. `git push` is not the REST API: no per-request rate
limit, and our files (50 KB - 2 MB) are far below GitHub's 100 MB file limit.

Why worktree-free: the archive contains Telegram filenames that are ILLEGAL on
Windows NTFS (colons, question marks), so the repo can never be checked out on
a Windows machine, and a --no-checkout clone has an EMPTY index - a naive
commit would wipe the tree. The sequence below avoids both:

    clone --depth 1 --filter=blob:none --no-checkout   (metadata only)
    git read-tree HEAD                                 (full index, no blobs)
    git hash-object -w <tmpfile>                       (blob, no worktree)
    git update-index --add --cacheinfo 100644,<sha>,<path>
    git commit / git push

The commit tree always contains the full archive plus the new files; the push
sends only new blobs. No working tree exists, so illegal filenames and Windows
never matter, and no archive bytes are re-downloaded per run. (The blobless
clone was empirically verified against the live repo before shipping: a probe
commit kept all 4,248 existing files.)
"""
import os
import subprocess
import time

GIT_FLUSH_FILES = 100
GIT_FLUSH_SECS = 60


def _sh(args, cwd=None):
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_ASKPASS'] = 'echo'
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', env=env)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or '')[-400:]
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {tail}")
    return r.stdout


class GitArchive:
    """See module docstring. One instance per run; add() per file; flush()."""

    def __init__(self, workdir, token, owner, repo, branch='main'):
        self.workdir = workdir
        self.branch = branch
        self._url = f'https://x-access-token:{token}@github.com/{owner}/{repo}.git'
        self.tmpdir = os.path.join(workdir, '_stage')
        self.pending = 0
        self.last_push = 0.0
        self.pushed_total = 0
        self._n = 0

    def ensure(self):
        """Clone once per run. Reuses an existing clone (idempotent)."""
        if os.path.isdir(os.path.join(self.workdir, '.git')):
            return
        os.makedirs(self.workdir, exist_ok=True)
        _sh(['git', 'clone', '--depth', '1', '--filter=blob:none',
             '--no-checkout', '--single-branch', '--branch', self.branch,
             self._url, '.'], cwd=self.workdir)
        _sh(['git', 'config', 'user.name', 'mock-archiver'], cwd=self.workdir)
        _sh(['git', 'config', 'user.email',
             'archiver@users.noreply.github.com'], cwd=self.workdir)
        # Some archive paths contain NTFS-illegal characters (colons in
        # "Déjà_vu:..."-style names, committed earlier via the API). Git on
        # Windows refuses to even index them while core.protectNTFS is on.
        # Disabling it here is safe because our flow NEVER writes a worktree:
        # blobs go object-store only (hash-object), never to disk under that
        # path. On Linux CI this setting is ignored anyway.
        _sh(['git', 'config', 'core.protectNTFS', 'false'], cwd=self.workdir)
        # Materialise the index from HEAD WITHOUT downloading blobs. Skipping
        # this is fatal: the index starts empty and a commit would erase the
        # entire archive tree.
        _sh(['git', 'read-tree', 'HEAD'], cwd=self.workdir)
        os.makedirs(self.tmpdir, exist_ok=True)

    def add(self, rel_path, data):
        """Stage one file (bytes, or a source path that is consumed/moved)."""
        self._n += 1
        tmp = os.path.join(self.tmpdir, f'{self._n}.bin')
        os.makedirs(self.tmpdir, exist_ok=True)
        if isinstance(data, bytes):
            with open(tmp, 'wb') as fh:
                fh.write(data)
        else:
            os.replace(data, tmp)               # consumes the source file
        sha = _sh(['git', 'hash-object', '-w', tmp],
                  cwd=self.workdir).strip()
        _sh(['git', 'update-index', '--add', '--cacheinfo',
             f'100644,{sha},{rel_path}'], cwd=self.workdir)
        os.remove(tmp)
        self.pending += 1

    def flush(self, message=None):
        """Commit the staged index and push. Raises on failure; the staged
        index is kept if the push fails, so partials stay recoverable."""
        if self.pending == 0:
            return 0
        n = self.pending
        _sh(['git', 'commit', '-m',
             message or f'archive batch: {n} file(s)'], cwd=self.workdir)
        try:
            _sh(['git', 'push', 'origin', self.branch], cwd=self.workdir)
        except RuntimeError:
            # Push failed (e.g. the remote moved ahead). Retry once after
            # fetching the new head; the concurrency lock makes this rare.
            _sh(['git', 'fetch', '--depth', '1', 'origin', self.branch],
                cwd=self.workdir)
            _sh(['git', 'rebase', 'FETCH_HEAD'], cwd=self.workdir)
            _sh(['git', 'push', 'origin', self.branch], cwd=self.workdir)
        self.pending = 0
        self.last_push = time.time()
        self.pushed_total += n
        return n

    def should_flush(self, force=False):
        if self.pending == 0:
            return False
        return force or self.pending >= GIT_FLUSH_FILES or \
            (time.time() - self.last_push) >= GIT_FLUSH_SECS
