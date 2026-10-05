"""Lifecycle hooks configured as local shell templates (start/stop/extend a VM)."""
from __future__ import annotations

from ..core import CloudsheepError, render, run

HOOKS = ('up', 'down', 'extend')


class HookMixin:
    """`up`, `down` and `extend` run user templates locally, e.g. `multipass start {instance}`."""

    def hook_values(self, **extra) -> dict:
        values = {key: value for key, value in self.settings.items() if isinstance(value, (str, int, float))}
        return {**values, 'name': self.name, **extra}

    def hook_capabilities(self, capabilities: set[str]) -> set[str]:
        return {cap for cap in capabilities if cap not in HOOKS or self.settings.get(cap)}

    def run_hook(self, hook: str, apply: bool, **extra) -> dict:
        template = self.settings.get(hook)
        if not template:
            raise CloudsheepError(f'{self.name} has no {hook} command configured')
        command = render(template, self.hook_values(**extra))
        if not apply:
            return {'machine': self.name, 'action': hook, 'applied': False, 'would_run': command}
        result = run(['sh', '-c', command])
        return {'machine': self.name, 'action': hook, 'applied': True, 'command': command,
                'output': result.stdout.strip()}

    def up(self, apply):
        return self.run_hook('up', apply)

    def down(self, apply, repo=None, branch=None):
        return self.run_hook('down', apply)

    def extend(self, duration):
        return self.run_hook('extend', True, duration=duration)
