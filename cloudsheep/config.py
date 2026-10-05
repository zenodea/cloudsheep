"""Load ~/.config/cloudsheep/machines.toml into Machine objects."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from .core import CloudsheepError, Machine
from .providers import DISCOVERY, PROVIDERS


def config_path() -> Path:
    if os.environ.get('CLOUDSHEEP_CONFIG'):
        return Path(os.environ['CLOUDSHEEP_CONFIG']).expanduser()
    base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')).expanduser()
    return base / 'cloudsheep' / 'machines.toml'


def load_raw(path: Path | None = None) -> dict:
    path = path or config_path()
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as error:
        raise CloudsheepError(f'{path}: {error}') from None


def machines(raw: dict | None = None) -> dict[str, Machine]:
    raw = load_raw() if raw is None else raw
    providers = raw.get('providers', {})
    result: dict[str, Machine] = {}
    for name, settings in raw.get('machines', {}).items():
        provider = settings.get('provider')
        if provider not in PROVIDERS:
            raise CloudsheepError(f'machine {name}: unknown provider {provider!r} (choose {", ".join(PROVIDERS)})')
        result[name] = PROVIDERS[provider](name, settings, providers.get(provider, {}))
    for provider, discover in DISCOVERY.items():
        if provider not in providers or providers[provider].get('discover') is False:
            continue
        configured = {getattr(m, 'task', None) for m in result.values() if m.provider == provider}
        for name, settings in discover(providers[provider]).items():
            if settings.get('task') in configured:
                continue
            if name in result:
                raise CloudsheepError(f'discovered {provider} machine {name} clashes with a configured machine; '
                                      f'set [providers.{provider}] prefix')
            result[name] = PROVIDERS[provider](name, settings, providers[provider])
    return result


def machine(name: str, raw: dict | None = None) -> Machine:
    found = machines(raw)
    if name not in found:
        known = ', '.join(sorted(found)) or f'none (edit {config_path()})'
        raise CloudsheepError(f'unknown machine {name!r}; known: {known}')
    return found[name]
