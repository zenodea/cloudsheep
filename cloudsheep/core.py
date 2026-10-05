"""Shared machine model, errors and subprocess helpers."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path


class CloudsheepError(Exception):
    """A diagnostic that is safe to show to the user."""


class Unsupported(CloudsheepError):
    pass


def state_dir() -> Path:
    base = os.environ.get('CLOUDSHEEP_STATE_DIR')
    if base:
        return Path(base).expanduser()
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')).expanduser() / 'cloudsheep'


def run(argv, *, input=None, check=True, timeout=None, cwd=None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        raise CloudsheepError(f'command not found: {argv[0]}') from None
    except subprocess.TimeoutExpired:
        raise CloudsheepError(f'{Path(argv[0]).name} timed out after {timeout}s') from None
    if check and result.returncode:
        raise CloudsheepError(failure(argv, result))
    return result


def failure(argv, result) -> str:
    """Prefer a structured {"ok": false, "error": ...} reply, then the last stderr line."""
    for stream in (result.stdout, result.stderr):
        try:
            value = json.loads(stream)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and isinstance(value.get('error'), str):
            return value['error']
    lines = [line for line in (result.stderr or result.stdout or '').strip().splitlines() if line.strip()]
    tail = lines[-1] if lines else 'no output'
    return f'{Path(argv[0]).name} exited {result.returncode}: {tail}'


def run_json(argv, **kwargs):
    result = run(argv, **kwargs)
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise CloudsheepError(f'{Path(argv[0]).name} did not return JSON') from None


def remote_path(path: str) -> str:
    """Shell-quote a remote path while letting a leading ~/ expand."""
    if path == '~':
        return '~'
    if path.startswith('~/'):
        return '~/' + shlex.quote(path[2:])
    return shlex.quote(path)


def render(template: str, values: dict) -> str:
    """Fill {placeholder}s with shell-quoted values ({cmd} is already-joined argv).

    Only known names are replaced, so other braces (JSON, awk, jq) pass through untouched.
    """
    def replace(match):
        key = match[1]
        if key not in values:
            return match[0]
        return values[key] if key == 'cmd' else shlex.quote(str(values[key]))
    return re.sub(r'\{([A-Za-z_][A-Za-z0-9_]*)\}', replace, template)


CAPABILITIES = ('status', 'shell', 'run', 'tunnel', 'sync', 'collect', 'up', 'down', 'extend',
                'jobs', 'ssh_config', 'native')


class Machine:
    """One VM. Providers override the operations they support."""

    provider = 'base'
    ports: dict = {}

    def __init__(self, name: str, settings: dict, provider_settings: dict | None = None):
        self.name = name
        self.settings = settings
        self.provider_settings = provider_settings or {}

    # Introspection -------------------------------------------------------
    def describe(self) -> str:
        return self.settings.get('description', '')

    def capabilities(self) -> set[str]:
        methods = {'status': 'status', 'shell': 'shell_argv', 'run': 'run_argv', 'tunnel': 'tunnel_argv',
                   'sync': 'sync', 'collect': 'collect', 'up': 'up', 'down': 'down', 'extend': 'extend',
                   'jobs': 'submit_job', 'ssh_config': 'ssh_config', 'native': 'native_argv'}
        return {cap for cap, method in methods.items()
                if getattr(type(self), method) is not getattr(Machine, method)}

    def require(self, capability: str):
        if capability not in self.capabilities():
            raise Unsupported(f'{self.name} ({self.provider}) does not support {capability}')

    def port_presets(self) -> dict:
        return {**self.ports, **{k: int(v) for k, v in self.settings.get('ports', {}).items()}}

    def shell_notice(self) -> str | None:
        return None

    # Operations ------------------------------------------------------------
    def status(self) -> dict:
        raise Unsupported('status')

    def shell_argv(self, as_self: bool = False) -> list[str]:
        raise Unsupported('shell')

    def run_argv(self, command: list[str], tty: bool = False) -> list[str]:
        raise Unsupported('run')

    def tunnel_argv(self, local: int, remote: int, remote_host: str = '127.0.0.1') -> list[str]:
        raise Unsupported('tunnel')

    def sync(self, repo: Path, include: list[str]) -> dict:
        raise Unsupported('sync')

    def collect(self, repo: Path, branch: str | None, include: list[str]) -> dict:
        raise Unsupported('collect')

    def up(self, apply: bool) -> dict:
        raise Unsupported('up')

    def down(self, apply: bool, repo: Path | None, branch: str | None) -> dict:
        raise Unsupported('down')

    def down_wants_repo(self, explicit: bool) -> bool:
        """Whether `down` should collect into a local repository first."""
        return False

    def extend(self, duration: str, apply: bool) -> dict:
        raise Unsupported('extend')

    def submit_job(self, command: list[str], job: str) -> dict:
        raise Unsupported('jobs')

    def job_status(self, job: str) -> dict:
        raise Unsupported('jobs')

    def job_cancel(self, job: str) -> dict:
        raise Unsupported('jobs')

    def job_logs(self, job: str, stream: str, offset: int) -> dict:
        """Return {'text', 'next_offset'}."""
        raise Unsupported('jobs')

    def ssh_config(self) -> dict | None:
        raise Unsupported('ssh_config')

    def native_argv(self, args: list[str]) -> list[str]:
        raise Unsupported('native')
