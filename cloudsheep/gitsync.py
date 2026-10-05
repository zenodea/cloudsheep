"""Provider-neutral source transfer over rsync, collected back as a new Git branch.

sync   copies tracked and nonignored untracked files (minus secret-looking paths)
       from a local repository to a remote directory. Nothing is deleted remotely.
collect copies the remote directory into a throwaway clone at the synced base
       commit, commits what the machine changed and fetches it as a new local
       branch. Files that come back exactly as they were synced are left at the
       base commit, so local uncommitted or untracked files the machine didn't
       touch stay out of the branch. The local HEAD, index and working tree are
       never touched.

This is not a secret scanner: review what you sync.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shlex
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .core import CloudsheepError, run, state_dir

SECRET_PATTERNS = ['.env', '.env.*', '*.env', '*.pem', '*.key', '*.p12', 'id_rsa*', 'id_ed25519*', 'id_ecdsa*',
                   '.netrc', '.ssh/', '.aws/', '.gnupg/', '.docker/config.json']
SECRET_ALLOW = ['.env.example', '.env.sample', '.env.template']


def git(repo: Path, *args, check=True):
    return run(['git', '-C', str(repo), *args], check=check)


def toplevel(path: Path) -> Path:
    result = run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'], check=False)
    if result.returncode:
        raise CloudsheepError(f'{path} is not inside a Git repository; pass --repo')
    return Path(result.stdout.strip())


def excluded(path: str, patterns: list[str]) -> bool:
    parts = path.split('/')
    if parts[-1] in SECRET_ALLOW:
        return False
    for pattern in patterns:
        if pattern.endswith('/'):
            if pattern[:-1] in parts[:-1]:
                return True
        elif '/' in pattern:
            if fnmatch.fnmatch(path, pattern):
                return True
        elif fnmatch.fnmatch(parts[-1], pattern):
            return True
    return False


def files(repo: Path, patterns: list[str]) -> tuple[list[str], list[str]]:
    listed = git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').stdout.split('\0')
    keep, skipped = [], []
    for path in sorted({p for p in listed if p}):
        if not os.path.lexists(repo / path):
            continue  # deleted tracked file
        (skipped if excluded(path, patterns) else keep).append(path)
    return keep, skipped


def hashes(repo: Path, paths: list[str]) -> dict[str, str]:
    """Blob ids as git would store them (no objects are written)."""
    if not paths:
        return {}
    out = git_input(repo, ['hash-object', '--stdin-paths'], '\n'.join(paths) + '\n').split()
    return dict(zip(paths, out))


def git_input(repo: Path, args: list[str], text: str) -> str:
    return run(['git', '-C', str(repo), *args], input=text).stdout


def rsync_filters(patterns: list[str]) -> list[str]:
    rules = [f'--include={name}' for name in SECRET_ALLOW]
    for pattern in patterns:
        rules.append(f'--exclude={pattern}')
    return rules


def _state_file(machine: str) -> Path:
    return state_dir() / 'sync' / (machine.replace('/', '_').replace(':', '_') + '.json')


def sync(machine: str, repo: Path, rsh: list[str], target: str, workdir: str, mkdir_argv: list[str],
         patterns: list[str]) -> dict:
    repo = toplevel(repo)
    keep, skipped = files(repo, patterns)
    if not keep:
        raise CloudsheepError('nothing to sync')
    base = git(repo, 'rev-parse', 'HEAD').stdout.strip()
    dirty = bool(git(repo, 'status', '--porcelain').stdout.strip())
    run(mkdir_argv)
    run(['rsync', '-a', '--from0', '--files-from=-', '-e', shlex.join(rsh),
         str(repo) + '/', f'{target}:{workdir.rstrip("/")}/'], input='\0'.join(keep) + '\0')
    record = {'machine': machine, 'repo': str(repo), 'base': base, 'dirty': dirty, 'workdir': workdir,
              'files': len(keep), 'synced_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    # What was sent, so collect can tell the machine's changes from local ones.
    synced = hashes(repo, keep) if dirty else {}
    path = _state_file(machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**record, 'synced': synced}) + '\n')
    return {**record, 'skipped_secret_like': skipped}


def _restore_untouched(clone: Path, base: str, synced: dict[str, str]) -> int:
    """Put files that came back exactly as synced back to their base state."""
    present = [path for path in synced if os.path.lexists(clone / path) and not os.path.islink(clone / path)]
    now = hashes(clone, present)
    untouched = [path for path in present if now.get(path) == synced[path]]
    if not untouched:
        return 0
    in_base = set(git(clone, 'ls-tree', '-r', '-z', '--name-only', base).stdout.split('\0')) - {''}
    for path in untouched:
        if path not in in_base:
            (clone / path).unlink()
    restore = [path for path in untouched if path in in_base]
    if restore:
        git_input(clone, ['checkout', base, '--pathspec-from-file=-', '--pathspec-file-nul'],
                  '\0'.join(restore) + '\0')
    return len(untouched)


def collect(machine: str, repo: Path, branch: str | None, rsh: list[str], target: str, workdir: str,
            patterns: list[str]) -> dict:
    repo = toplevel(repo)
    path = _state_file(machine)
    if not path.exists():
        raise CloudsheepError(f'no recorded sync for {machine}; sync first so the base commit is known')
    record = json.loads(path.read_text())
    base = record['base']
    if git(repo, 'cat-file', '-e', base + '^{commit}', check=False).returncode:
        raise CloudsheepError(f'base commit {base[:12]} is not in {repo}')
    branch = branch or f'cloudsheep/{machine.replace(":", "-")}-{time.strftime("%Y%m%d-%H%M%S")}'
    git(repo, 'check-ref-format', '--branch', branch)
    if git(repo, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch, check=False).returncode == 0:
        raise CloudsheepError(f'branch {branch} already exists')
    with tempfile.TemporaryDirectory(prefix='cloudsheep-collect-') as temporary:
        clone = Path(temporary) / 'clone'
        run(['git', 'clone', '-q', '--no-checkout', '--shared', str(repo), str(clone)])
        git(clone, 'checkout', '-q', '--detach', base)
        # Skip root-.gitignore'd output (node_modules, target, ...) during transfer; `git add -A` below
        # applies the full ignore rules anyway. rsync cannot express `!` re-includes, so a .gitignore
        # with negations is not used for transfer at all (slower, never misses a change).
        lines = [line.strip() for line in git(repo, 'show', base + ':.gitignore', check=False).stdout.splitlines()]
        rules = [line for line in lines if line and not line.startswith('#')]
        ignore = Path(temporary) / 'ignore'
        ignore.write_text('' if any(r.startswith('!') for r in rules) else ''.join(r + '\n' for r in rules))
        run(['rsync', '-a', '--delete', '--exclude=.git', *rsync_filters(patterns), f'--exclude-from={ignore}',
             '-e', shlex.join(rsh), f'{target}:{workdir.rstrip("/")}/', str(clone) + '/'])
        _restore_untouched(clone, base, record.get('synced', {}))
        git(clone, 'add', '-A')
        if git(clone, 'diff', '--cached', '--quiet', check=False).returncode == 0:
            return {'machine': machine, 'base': base, 'changed': False, 'branch': None}
        # The clone does not inherit repo-local config, so carry the repo's identity over.
        name = git(repo, 'config', 'user.name', check=False).stdout.strip() or 'cloudsheep'
        email = git(repo, 'config', 'user.email', check=False).stdout.strip() or 'cloudsheep@localhost'
        identity = ['-c', f'user.name={name}', '-c', f'user.email={email}']
        run(['git', '-C', str(clone), *identity, '-c', 'commit.gpgsign=false', 'commit', '-q', '--no-verify', '-m', f'Collect {machine} from {workdir}'])
        stat = git(clone, 'diff', '--stat', base, 'HEAD').stdout.strip()
        git(repo, 'fetch', '-q', str(clone), 'HEAD:refs/heads/' + branch)
    commit = git(repo, 'rev-parse', branch).stdout.strip()
    return {'machine': machine, 'base': base, 'changed': True, 'branch': branch, 'commit': commit, 'stat': stat}
