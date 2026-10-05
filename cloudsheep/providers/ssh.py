"""Any machine reachable over SSH: cloud VMs, Multipass/Lima/Tart guests, a box under the desk."""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from .. import gitsync
from ..core import CloudsheepError, Machine, remote_path, run
from .hooks import HookMixin

JOB = re.compile(r'[a-z0-9][a-z0-9-]{0,47}')
JOBS = '"$HOME"/.cloudsheep/jobs'


def job_dir(job: str) -> str:
    if not JOB.fullmatch(job):
        raise CloudsheepError('job ids use lowercase letters, digits and hyphens (max 48)')
    return f'{JOBS}/{job}'


class SshMachine(HookMixin, Machine):
    provider = 'ssh'

    def __init__(self, name, settings, provider_settings=None):
        super().__init__(name, settings, provider_settings)
        if not settings.get('host'):
            raise CloudsheepError(f'machine {name}: ssh provider needs host')

    def describe(self):
        return self.settings.get('description') or self.target()

    def capabilities(self):
        return self.hook_capabilities(super().capabilities())

    # Connection ------------------------------------------------------------
    @property
    def workdir(self) -> str:
        return self.settings.get('workdir', f'~/cloudsheep/{self.name}')

    def target(self) -> str:
        user = self.settings.get('user')
        return f'{user}@{self.settings["host"]}' if user else self.settings['host']

    def ssh_base(self) -> list[str]:
        argv = ['ssh']
        if self.settings.get('port'):
            argv += ['-p', str(self.settings['port'])]
        if self.settings.get('identity_file'):
            argv += ['-i', str(Path(self.settings['identity_file']).expanduser())]
        if self.settings.get('proxy_jump'):
            argv += ['-J', self.settings['proxy_jump']]
        if self.settings.get('proxy_command'):
            argv += ['-o', 'ProxyCommand=' + self.settings['proxy_command']]
        argv += ['-o', 'ForwardAgent=' + ('yes' if self.settings.get('forward_agent') else 'no')]
        for option in self.settings.get('options', []):
            argv += ['-o', option]
        return argv

    def ssh(self, command: str, tty: bool = False, batch: bool = False) -> list[str]:
        flags = ['-t'] if tty else ['-T']
        if batch:
            flags += ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
        return [*self.ssh_base(), *flags, self.target(), command]

    def in_workdir(self, command: str) -> str:
        return f'cd {remote_path(self.workdir)} && {command}'

    # Operations ------------------------------------------------------------
    def status(self):
        probe = 'printf "%s\\n" "$(uname -sr)" "$(uptime)"'
        try:
            result = run(self.ssh(probe, batch=True), check=False, timeout=25)
        except CloudsheepError as error:
            return {'machine': self.name, 'state': 'unknown', 'error': str(error)}
        if result.returncode:
            return {'machine': self.name, 'state': 'unreachable', 'error': (result.stderr.strip().splitlines() or [''])[-1]}
        lines = result.stdout.strip().splitlines()
        return {'machine': self.name, 'state': 'reachable', 'kernel': lines[0] if lines else '',
                'uptime': lines[1].strip() if len(lines) > 1 else '', 'workdir': self.workdir, 'jobs': self.jobs()}

    def shell_argv(self, as_self=False):
        if as_self:
            return [*self.ssh_base(), '-t', self.target()]
        return self.ssh(f'cd {remote_path(self.workdir)} 2>/dev/null || echo "cloudsheep: {self.workdir} '
                        f'does not exist yet (sync first)"; exec "${{SHELL:-sh}}" -l', tty=True)

    def run_argv(self, command, tty=False):
        return self.ssh(self.in_workdir(shlex.join(command)), tty=tty)

    def tunnel_argv(self, local, remote, remote_host='127.0.0.1'):
        return [*self.ssh_base(), '-N', '-o', 'ExitOnForwardFailure=yes',
                '-L', f'127.0.0.1:{local}:{remote_host}:{remote}', self.target()]

    def patterns(self):
        return gitsync.SECRET_PATTERNS + list(self.settings.get('exclude', []))

    def sync(self, repo, include):
        if include:
            raise CloudsheepError('ssh sync already sends every nonignored file; --include is not needed')
        mkdir = self.ssh(f'mkdir -p {remote_path(self.workdir)}', batch=True)
        return gitsync.sync(self.name, repo, self.ssh_base(), self.target(), self.workdir, mkdir, self.patterns())

    def collect(self, repo, branch, include):
        return gitsync.collect(self.name, repo, branch, self.ssh_base(), self.target(), self.workdir, self.patterns())

    # Detached jobs: nohup/setsid under ~/.cloudsheep/jobs/<id>, survive disconnects.
    def submit_job(self, command, job):
        folder = job_dir(job)
        inner = self.in_workdir(shlex.join(command))
        outer = (f'echo $$ > {folder}/pid; sh -c {shlex.quote(inner)}; '
                 f'echo $? > {folder}/exit.tmp && mv {folder}/exit.tmp {folder}/exit')
        detach = f'sh -c {shlex.quote(outer)} >{folder}/stdout 2>{folder}/stderr </dev/null &'
        script = (f'if [ -e {folder} ]; then echo "job {job} already exists" >&2; exit 3; fi; '
                  f'mkdir -p {folder} && printf "%s\\n" {shlex.quote(shlex.join(command))} > {folder}/command && '
                  f'if command -v setsid >/dev/null 2>&1; then setsid nohup {detach} else nohup {detach} fi; '
                  f'echo $! > {folder}/pid')
        run(self.ssh(script, batch=True))
        return {'machine': self.name, 'job': job, 'state': 'submitted', 'command': command}

    STATUS = ('if [ -f "$d/exit" ]; then echo "completed $(cat "$d/exit")"; '
              'elif [ -f "$d/pid" ] && kill -0 "$(cat "$d/pid")" 2>/dev/null; then echo running; '
              'elif [ -d "$d" ]; then echo interrupted; else echo missing; fi')

    @staticmethod
    def parse_state(line: str) -> dict:
        state, _, code = line.strip().partition(' ')
        return {'state': state or 'unknown', 'exit_code': int(code) if code.strip().lstrip('-').isdigit() else None}

    def job_status(self, job):
        result = run(self.ssh(f'd={job_dir(job)}; {self.STATUS}', batch=True))
        return {'machine': self.name, 'job': job, **self.parse_state(result.stdout)}

    def jobs(self):
        script = f'for d in {JOBS}/*/; do [ -d "$d" ] || continue; d=${{d%/}}; printf "%s " "${{d##*/}}"; {self.STATUS}; done'
        result = run(self.ssh(script, batch=True), check=False, timeout=25)
        jobs = []
        for line in result.stdout.splitlines():
            job, _, rest = line.partition(' ')
            if job:
                jobs.append({'job': job, **self.parse_state(rest)})
        return jobs

    def job_logs(self, job, stream, offset, limit=65536):
        if stream not in ('stdout', 'stderr'):
            raise CloudsheepError('stream must be stdout or stderr')
        command = f'tail -c +{int(offset) + 1} {job_dir(job)}/{stream} 2>/dev/null | head -c {int(limit)}'
        result = subprocess.run(self.ssh(command, batch=True), capture_output=True)
        if result.returncode:
            raise CloudsheepError(f'could not read logs for {job}')
        return {'text': result.stdout.decode('utf-8', 'replace'), 'next_offset': offset + len(result.stdout)}

    def job_cancel(self, job):
        folder = job_dir(job)
        run(self.ssh(f'p=$(cat {folder}/pid 2>/dev/null) && (kill -TERM -- "-$p" 2>/dev/null || kill -TERM "$p")',
                     batch=True), check=False)
        return self.job_status(job)

    def ssh_config(self):
        entry = {'HostName': self.settings['host']}
        for key, option in (('user', 'User'), ('port', 'Port'), ('identity_file', 'IdentityFile'),
                            ('proxy_jump', 'ProxyJump'), ('proxy_command', 'ProxyCommand')):
            if self.settings.get(key):
                entry[option] = str(self.settings[key])
        entry['ForwardAgent'] = 'yes' if self.settings.get('forward_agent') else 'no'
        return {'host': self.settings.get('alias', self.name), 'options': entry}
