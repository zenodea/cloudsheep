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
    # zone = "us-central1-a"                   # override the kit config's zone

`zone` (here or in [providers.gcp-worker]) applies to new leases only and must
stay in the region of the config's subnet. Once a lease exists, every agent.py
call uses the lease's own config, as agent.py requires.
"""
from __future__ import annotations

import fcntl
import sys
import hashlib
import json
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path

from ..core import CloudsheepError, Machine, run, run_json, state_dir

TASK = re.compile(r'[a-z0-9][a-z0-9-]{0,47}')
REMOTE_ROOT = '/home/agent/.local/share/bundle-worker-agent/tasks'
LEASE_STATE = '~/.local/state/bundle-worker-agent'
STARTUP_WAIT = 20 * 60   # a fresh worker's startup script takes several minutes
STARTUP_POLL = 20
ZONE = re.compile(r'[a-z]+-[a-z]+[0-9]+-[a-z]')


def lease_dir(provider_settings: dict) -> Path:
    # agent.py always uses LEASE_STATE; `state_dir` exists for tests and must otherwise stay unset.
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

    def setting(self, key):
        return self.settings.get(key) or self.provider_settings.get(key)

    def agent_config(self) -> Path | None:
        """The --config for agent.py: the lease's own config once there is one, else the
        configured file (or the kit default) with `zone` applied."""
        lease = self.lease()
        if lease and not lease.get('released') and isinstance(lease.get('config'), dict):
            config = lease['config']
        else:
            base = self.setting('config')
            zone = self.setting('zone')
            if not zone:
                return Path(base).expanduser() if base else None
            if not ZONE.fullmatch(str(zone)):
                raise CloudsheepError(f'{self.name}: invalid zone {zone!r}')
            path = Path(base).expanduser() if base else self.kit() / 'config.example.json'
            try:
                config = {**json.loads(path.read_text()), 'zone': zone}
            except (OSError, ValueError) as error:
                raise CloudsheepError(f'cannot read gcp-worker config {path}: {error}') from None
        text = json.dumps(config, indent=2, sort_keys=True) + '\n'
        folder = state_dir() / 'gcp-worker'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"config-{hashlib.sha256(text.encode()).hexdigest()[:12]}.json"
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
        return path

    def agent(self, action: str, *args: str, dry_run: bool = False) -> list[str]:
        argv = [self.provider_settings.get('python', 'python3'), '-B', str(self.kit() / 'agent.py'), action]
        config = self.agent_config()
        if config:
            argv += ['--config', str(config)]
        if dry_run:
            argv.append('--dry-run')
        return [*argv, '--task', self.task, *args]

    def lease_path(self) -> Path:
        return lease_dir(self.provider_settings) / f'lease-{self.task}.json'

    def lease(self) -> dict | None:
        path = self.lease_path()
        if not path.exists():
            return None
        return json.loads(path.read_text())

    @staticmethod
    def pending(lease) -> bool:
        """acquire started (the reservation is saved) but never bound a VM, e.g. no GPU capacity."""
        return bool(lease and not lease.get('released') and not lease.get('instance_id'))

    @staticmethod
    def starting(lease) -> bool:
        """A VM is bound but acquire hasn't installed the helper yet (startup script still running)."""
        return bool(lease and not lease.get('released') and lease.get('instance_id') and not lease.get('helper_installed'))

    def pending_hint(self) -> str:
        return (f'acquire did not finish; `cloudsheep up {self.name} --yes` retries it, '
                f'`cloudsheep down {self.name} --yes` drops it if no VM was created')

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
            kind = 'pending' if self.pending(lease) else 'adopted' if lease.get('adopted') else 'created'
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
        if self.pending(lease):
            return {'machine': self.name, 'task': self.task, 'state': 'pending', 'vm': lease['name'],
                    'zone': lease['config']['zone'], 'hint': self.pending_hint()}
        if self.starting(lease):
            return {'machine': self.name, 'task': self.task, 'state': 'starting', 'vm': lease['name'],
                    'zone': lease['config']['zone'], 'hard_deadline': lease.get('native_termination_time'),
                    'hint': f'the VM exists but acquire has not finished; `cloudsheep up {self.name} --yes` '
                            'resumes it and waits for startup'}
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
        deadline = time.monotonic() + STARTUP_WAIT
        try:
            while True:
                try:
                    value = run_json(self.agent('acquire', *args), timeout=1800)
                    break
                except CloudsheepError as error:
                    # The VM exists and acquire is resumable: wait out the startup script.
                    if not self.starting(self.lease()) or time.monotonic() > deadline:
                        raise
                    if self.startup_failed():
                        raise CloudsheepError(
                            f'{error}\n{self.name}: the VM\'s bundle-worker service failed, so it will never become '
                            f'ready. Look with `cloudsheep shell {self.name} --self`, then `sudo journalctl -u '
                            f'bundle-worker` and `sudo tail /run/bundle-worker/app.log`; `cloudsheep down '
                            f'{self.name} --yes` deletes it.') from None
                    print(f'cloudsheep: {self.name} is starting up ({error}); retrying in {STARTUP_POLL}s',
                          file=sys.stderr, flush=True)
                    time.sleep(STARTUP_POLL)
        except CloudsheepError as error:
            if self.pending(self.lease()):
                raise CloudsheepError(f'{error}\n{self.name}: {self.pending_hint()}. agent.py hides the failing '
                                      'command\'s output; GPU capacity in the zone is a common cause '
                                      '(set zone = "..." for a new lease).') from None
            raise
        return {'machine': self.name, 'action': 'up', 'applied': True, **value}

    def down_wants_repo(self, explicit):
        # Created workers must be collected before deletion; adopted ones only when asked.
        lease = self.lease()
        if self.pending(lease):
            return False
        return explicit or bool(lease and not lease.get('released') and not lease.get('adopted'))

    def startup_failed(self) -> bool:
        """True only when the VM positively reports its bundle-worker service as failed."""
        try:
            result = run([*self.gcloud_ssh(), '--ssh-flag=-oBatchMode=yes', '--ssh-flag=-T',
                          '--command=systemctl is-failed bundle-worker'], check=False, timeout=90, input='')
        except CloudsheepError:
            return False
        return result.stdout.strip().splitlines()[-1:] == ['failed']

    def vm_exists(self, lease) -> bool:
        config = lease['config']
        names = run(['gcloud', 'compute', 'instances', 'list', f"--project={config['project']}",
                     f"--filter=name=({lease['name']})", '--format=value(name)'], timeout=120).stdout.split()
        return lease['name'] in names

    def forget_pending(self, apply):
        """Drop a reservation that never bound a VM; agent.py has no command for this."""
        lease = self.lease()
        if self.vm_exists(lease):
            raise CloudsheepError(f"{self.name}: acquire did not finish but VM {lease['name']} exists; "
                                  f'resume with `cloudsheep up {self.name} --yes`')
        path = self.lease_path()
        effect = f"no VM {lease['name']} exists; forgets the unfinished lease (nothing to collect or delete)"
        plan = {'machine': self.name, 'action': 'down', 'applied': apply, 'effect': effect,
                'would_run': f'mv {path} {path.name}.abandoned-<time>'}
        if not apply:
            return plan
        with (path.parent / 'fleet.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.pending(self.lease()):
                raise CloudsheepError(f'{self.name}: the lease changed meanwhile; check `cloudsheep status {self.name}`')
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            path.rename(path.with_name(f'{path.name}.abandoned-{stamp}'))
        return plan

    def down(self, apply, repo=None, branch=None):
        if self.pending(self.lease()):
            return self.forget_pending(apply)
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
            raise CloudsheepError('set ssh_user in [providers.gcp-worker]; find it with: gcloud compute '
                                  "os-login describe-profile --format='value(posixAccounts[0].username)'")
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
