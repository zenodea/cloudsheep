"""Provider registry. A provider is a Machine subclass plus optional discovery."""
from __future__ import annotations

from .command import CommandMachine
from .gcp_worker import GcpWorkerMachine, discover as discover_gcp
from .ssh import SshMachine

PROVIDERS = {
    'ssh': SshMachine,
    'command': CommandMachine,
    'gcp-worker': GcpWorkerMachine,
}

# Providers that can list machines without explicit config, enabled by their [providers.X] table.
DISCOVERY = {
    'gcp-worker': discover_gcp,
}
