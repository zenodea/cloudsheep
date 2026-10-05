"""Adapter for the bundle repo's disposable GCP workers (scripts/gcp-worker/agent.py).

agent.py stays the authority for leases, identity checks, sync/collect and
deletion; this adapter only calls it and adds interactive SSH over IAP.
Unreleased leases in agent.py's state directory appear as machines named after
their task. Configure a machine explicitly to acquire a new one:

    [machines.heavy]
    provider = "gcp-worker"
    task = "heavy"
    existing_name = "bundle-image-test-02"     # adopt, or:
    # create = { lease = "1h", ttl = "4h" }    # billable creation
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from ..core import CloudsheepError, Machine, run, run_json

TASK = re.compile(r'[a-z0-9][a-z0-9-]{0,47}')
REMOTE_ROOT = '/home/agent/.local/share/bundle-worker-agent/tasks'
LEASE_STATE = '~/.local/state/bundle-worker-agent'


def lease_dir(provider_settings: dict) -> Path:
    return Path(provider_settings.get('state_dir', LEASE_STATE)).expanduser()


def discover(provider_settings: dict) -> dict[str, dict]:
    machines = {}
    folder = lease_dir(provider_settings)
    for path in sorted(folder.glob('lease-*.json')):
        try:
            lease = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not lease.get('released') and TASK.fullmatch(lease.get('task', '')):
            machines[provider_settings.get('prefix', '') + lease['task']] = {'task': lease['task']}
    return machines


class GcpWorkerMachine(Machine):
    provider = 'gcp-worker'
    ports = {'novnc': 6080}

    def __init__(self, name, settings, provider_settings=None):
        super().__init__(name, settings, provider_settings)
        self.task = settings.get('task', name)
        if not TASK.fullmatch(self.task):
            raise CloudsheepError(f'machine {name}: invalid gcp-worker task id {self.task!r}')

    # agent.py ----------------------------------------------------------------
    def kit(self) -> Path:
        kit = self.settings.get('kit') or self.provider_settings.get('kit')
        if not kit:
            raise CloudsheepError('set [providers.gcp-worker] kit = "/path/to/bundle/scripts/gcp-worker"')
        path = Path(kit).expanduser()
        if not (path / 'agent.py').is_file():
            raise CloudsheepError(f'{path}/agent.py not found; check the gcp-worker kit path')
        return path

    def agent(self, action: str, *args: str, dry_run: bool = False) -> list[str]:
        argv = [self.provider_settings.get('python', 'python3'), '-B', str(self.kit() / 'agent.py'), action]
        config = self.settings.get('config') or self.provider_settings.get('config')
        if config:
            argv += ['--config', str(Path(config).expanduser())]
        if dry_run:
            argv.append('--dry-run')
        return [*argv, '--task', self.task, *args]

    def lease(self) -> dict | None:
        path = lease_dir(self.provider_settings) / f'lease-{self.task}.json'
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def bound_lease(self) -> dict:
        lease = self.lease()
        if not lease or lease.get('released'):
            raise CloudsheepError(f'{self.name}: no active lease for task {self.task}; run `cloudsheep up {self.name}`')
        if not lease.get('instance_id'):
            raise CloudsheepError(f'{self.name}: lease is not bound to a VM yet; resume with `cloudsheep up {self.name}`')
        return lease

    def describe(self):
        lease = self.lease()
        if lease and not lease.get('released'):
            kind = 'adopted' if lease.get('adopted') else 'created'
            return f"{lease['name']} ({lease['config']['zone']}, {kind})"
        return self.settings.get('description', f'task {self.task} (no lease)')

    # IAP SSH -----------------------------------------------------------------
    def gcloud_ssh(self) -> list[str]:
        lease = self.bound_lease()
        config = lease['config']
        return ['gcloud', 'compute', 'ssh', lease['name'], f"--project={config['project']}", f"--zone={config['zone']}",
                '--tunnel-through-iap', '--ssh-flag=-oForwardAgent=no', '--ssh-flag=-oForwardX11=no']

    def source(self) -> str:
        return f'{REMOTE_ROOT}/{self.task}/source'

    def as_agent(self, command: str) -> str:
        return 'sudo -n runuser -l agent -c ' + shlex.quote(command)

    def shell_notice(self):
        return ('Interactive edits are invisible to agent.py job tracking: exit this shell before '
                'collect/release, and commit or keep new files tracked so collection picks them up.')

    # Operations ----------------------------------------------------------------
    def status(self):
        lease = self.lease()
        if not lease or lease.get('released'):
            return {'machine': self.name, 'task': self.task, 'state': 'absent' if not lease else 'released'}
        value = run_json(self.agent('status'), timeout=120)
        return {'machine': self.name, 'task': self.task, 'state': value.get('vm_status', 'unknown'),
                'vm': lease['name'], 'zone': lease['config']['zone'], 'adopted': bool(lease.get('adopted')),
                'expires_at': value.get('expires_at'), 'hard_deadline': lease.get('native_termination_time'),
                'commit': value.get('commit'), 'source': value.get('source'),
                'jobs': [{'job': job.get('job'), 'state': job.get('state'), 'exit_code': job.get('exit_code'),
                          'agent': (job.get('agent_run') or {}).get('agent')} for job in value.get('jobs', [])]}

    def shell_argv(self, as_self=False):
        if as_self:
            return [*self.gcloud_ssh(), '--ssh-flag=-t']
        inner = (f'cd {shlex.quote(self.source())} 2>/dev/null || '
                 f'echo "cloudsheep: task source not synced yet"; exec bash -l')
        return [*self.gcloud_ssh(), '--ssh-flag=-t', '--command=' + self.as_agent(inner)]

    def run_argv(self, command, tty=False):
        inner = f'cd {shlex.quote(self.source())} && exec {shlex.join(command)}'
        return [*self.gcloud_ssh(), '--ssh-flag=' + ('-t' if tty else '-T'), '--command=' + self.as_agent(inner)]

    def tunnel_argv(self, local, remote, remote_host='127.0.0.1'):
        return [*self.gcloud_ssh(), '--ssh-flag=-N', '--ssh-flag=-oExitOnForwardFailure=yes',
                f'--ssh-flag=-L127.0.0.1:{local}:{remote_host}:{remote}']

    def sync(self, repo, include):
        args = ['--repo', str(repo)]
        for path in include:
            args += ['--include-untracked', path]
        return run_json(self.agent('sync', *args))

    def collect(self, repo, branch, include):
        args = ['--repo', str(repo)]
        for path in include:
            args += ['--include-untracked', path]
        if branch:
            args += ['--branch', branch]
        return run_json(self.agent('collect', *args))

    def acquire_args(self) -> tuple[list[str], bool]:
        lease = self.lease()
        if lease and not lease.get('released'):
            create = not lease.get('adopted')
            args = ['--approve-create'] if create else []
            settings = self.settings.get('create', {}) if create else {}
        elif self.settings.get('existing_name'):
            return ['--existing-name', self.settings['existing_name']], False
        elif isinstance(self.settings.get('create'), dict):
            create, args, settings = True, ['--approve-create'], self.settings['create']
        else:
            raise CloudsheepError(f'{self.name}: configure existing_name (adopt) or a create table to acquire')
        for key, flag in (('lease', '--lease'), ('ttl', '--ttl'), ('image', '--image'),
                          ('source_ref', '--source-ref'), ('cap', '--cap')):
            if key in settings:
                args += [flag, str(settings[key])]
        return args, create

    def up(self, apply):
        args, create = self.acquire_args()
        if not apply:
            plan = run_json(self.agent('acquire', *args, dry_run=True))
            return {'machine': self.name, 'action': 'up', 'applied': False, 'billable_creation': create,
                    'would_run': shlex.join(self.agent('acquire', *args)), 'plan': plan.get('arguments')}
        return {'machine': self.name, 'action': 'up', 'applied': True, **run_json(self.agent('acquire', *args), timeout=1800)}

    def down_wants_repo(self, explicit):
        # Created workers must be collected before deletion; adopted ones only when asked.
        lease = self.lease()
        return explicit or bool(lease and not lease.get('released') and not lease.get('adopted'))

    def down(self, apply, repo=None, branch=None):
        lease = self.bound_lease()
        args = []
        if repo is not None:
            args += ['--repo', str(repo)]
        elif not lease.get('adopted'):
            raise CloudsheepError('releasing a created worker collects first and deletes the VM; pass --repo')
        if branch:
            args += ['--branch', branch]
        collect = 'collects into a new branch, then ' if repo is not None else ''
        effect = (f'{collect}relinquishes the lease; the VM is kept' if lease.get('adopted')
                  else f'{collect}DELETES the VM')
        if not apply:
            return {'machine': self.name, 'action': 'down', 'applied': False, 'effect': effect,
                    'would_run': shlex.join(self.agent('release', *args))}
        return {'machine': self.name, 'action': 'down', 'applied': True, 'effect': effect,
                **run_json(self.agent('release', *args), timeout=1800)}

    def extend(self, duration, apply):
        lease = self.bound_lease()
        if not apply:
            return {'machine': self.name, 'action': 'extend', 'applied': False,
                    'effect': f"soft lease set to {duration} from now, capped at {lease.get('native_termination_time')}",
                    'would_run': shlex.join(self.agent('renew', '--for', duration))}
        return {'machine': self.name, 'action': 'extend', 'applied': True,
                **run_json(self.agent('renew', '--for', duration))}

    def submit_job(self, command, job):
        return run_json(self.agent('exec', '--job', job, '--', *command))

    def job_status(self, job):
        return run_json(self.agent('status', '--job', job))

    def job_cancel(self, job):
        return run_json(self.agent('cancel', '--job', job))

    def job_logs(self, job, stream, offset, limit=65536):
        value = run_json(self.agent('logs', '--job', job, '--stream', stream, '--offset', str(offset),
                                    '--limit', str(limit)))
        return {'text': value.get('text', ''), 'next_offset': value.get('next_offset', offset)}

    def ssh_config(self):
        lease = self.bound_lease()
        config = lease['config']
        user = self.settings.get('ssh_user') or self.provider_settings.get('ssh_user')
        if not user:
            user = run(['gcloud', 'compute', 'os-login', 'describe-profile',
                        '--format=value(posixAccounts[0].username)'], timeout=60).stdout.strip()
        options = {
            'HostName': lease['name'],
            'User': user,
            'ProxyCommand': (f"gcloud compute start-iap-tunnel %h 22 --listen-on-stdin "
                             f"--project={config['project']} --zone={config['zone']} --verbosity=warning"),
            'IdentityFile': '~/.ssh/google_compute_engine',
            'HostKeyAlias': f"compute.{lease['instance_id']}",
            'UserKnownHostsFile': '~/.ssh/google_compute_known_hosts',
            'ForwardAgent': 'no',
        }
        return {'host': self.settings.get('alias', f'cs-{self.task}'), 'options': options}

    def native_argv(self, args):
        if not args:
            raise CloudsheepError('native needs an agent.py subcommand, e.g. run-agent --job ...')
        return self.agent(args[0], *args[1:])
