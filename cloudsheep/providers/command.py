"""Any VM tool with a CLI, described by shell templates (limactl, multipass, orb, tart, aws ssm, ...).

Every operation is optional; only configured templates become capabilities.
Placeholders are shell-quoted: {name}, any string setting (e.g. {instance}),
{cmd} (the command argv, already joined), {local_port}, {remote_port},
{remote_host}, {repo}, {branch}, {duration}.
"""
from __future__ import annotations

import json
import shlex

from ..core import CloudsheepError, Machine, render, run
from .hooks import HookMixin

TEMPLATES = {'status': 'status', 'shell': 'shell', 'run': 'run', 'tunnel': 'tunnel', 'sync': 'sync',
             'collect': 'collect', 'up': 'up', 'down': 'down', 'extend': 'extend', 'ssh_config': 'ssh'}


class CommandMachine(HookMixin, Machine):
    provider = 'command'

    def capabilities(self):
        return {cap for cap, key in TEMPLATES.items() if self.settings.get(key)}

    def describe(self):
        return self.settings.get('description') or self.settings.get('instance', '')

    def sh(self, key, **extra) -> list[str]:
        template = self.settings.get(key)
        if not template:
            raise CloudsheepError(f'{self.name} has no {key} command configured')
        return ['sh', '-c', render(template, self.hook_values(**extra))]

    def status(self):
        result = run(self.sh('status'), check=False, timeout=60)
        if result.returncode:
            return {'machine': self.name, 'state': 'unknown', 'error': (result.stderr.strip().splitlines() or [''])[-1]}
        text = result.stdout.strip()
        try:
            value = json.loads(text)
        except ValueError:
            return {'machine': self.name, 'state': 'ok', 'output': text}
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        return {'machine': self.name, 'state': 'ok', **(value if isinstance(value, dict) else {'output': value})}

    def shell_argv(self, as_self=False):
        return self.sh('shell')

    def run_argv(self, command, tty=False):
        if '{cmd}' not in self.settings.get('run', ''):
            raise CloudsheepError(f'{self.name}: the run template must contain {{cmd}}')
        return self.sh('run', cmd=shlex.join(command))

    def tunnel_argv(self, local, remote, remote_host='127.0.0.1'):
        return self.sh('tunnel', local_port=local, remote_port=remote, remote_host=remote_host)

    def sync(self, repo, include):
        result = run(self.sh('sync', repo=str(repo)))
        return {'machine': self.name, 'synced': True, 'output': result.stdout.strip()}

    def collect(self, repo, branch, include):
        result = run(self.sh('collect', repo=str(repo), branch=branch or ''))
        return {'machine': self.name, 'collected': True, 'output': result.stdout.strip()}

    def ssh_config(self):
        options = self.settings.get('ssh')
        if not isinstance(options, dict):
            raise CloudsheepError(f'{self.name}: ssh must be a table of ssh_config options')
        options = dict(options)
        return {'host': options.pop('Host', self.name), 'options': options}
