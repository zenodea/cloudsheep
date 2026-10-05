import json
import os
import unittest
from pathlib import Path

from cloudsheep import config
from cloudsheep.core import CloudsheepError, Unsupported
from helpers import Sandbox

FAKE_AGENT = r'''
import json, os, sys
with open(os.environ['FAKE_AGENT_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\n')
action = sys.argv[1]
if '--dry-run' in sys.argv:
    print(json.dumps({'ok': True, 'dry_run': True, 'arguments': {'action': action}}))
elif action == 'status' and '--job' in sys.argv:
    print(json.dumps({'ok': True, 'job': 'j1', 'state': 'completed', 'exit_code': 0}))
elif action == 'status':
    print(json.dumps({'ok': True, 'task': 'heavy', 'commit': 'abc', 'source': '/x', 'vm_status': 'RUNNING',
                      'expires_at': '2026-10-05T13:00:00Z',
                      'jobs': [{'job': 'j1', 'state': 'running', 'exit_code': None, 'agent_run': {'agent': 'codex'}}]}))
elif action == 'logs':
    print(json.dumps({'ok': True, 'text': 'hello', 'next_offset': 5}))
elif action == 'sync' and os.environ.get('FAKE_AGENT_REFUSE'):
    print(json.dumps({'ok': False, 'error': 'remote workspace has unsaved files; no overwrite'}))
    sys.exit(1)
else:
    print(json.dumps({'ok': True, 'action': action}))
'''


class CommandProviderTest(Sandbox):
    def test_templates_become_capabilities(self):
        self.config('''
[machines.lima]
provider = "command"
instance = "dev box"
status = "printf '[{\\"status\\": \\"Running\\", \\"name\\": \\"dev\\"}]'"
shell = "limactl shell {instance}"
run = "limactl shell {instance} -- {cmd}"
''')
        lima = config.machine('lima')
        self.assertEqual(lima.capabilities(), {'status', 'shell', 'run'})
        self.assertEqual(lima.shell_argv(), ['sh', '-c', "limactl shell 'dev box'"])
        self.assertEqual(lima.run_argv(['echo', 'a b']), ['sh', '-c', "limactl shell 'dev box' -- echo 'a b'"])
        self.assertEqual(lima.status()['status'], 'Running')
        with self.assertRaises(Unsupported):
            lima.require('sync')

    def test_run_template_must_use_cmd(self):
        self.config('[machines.x]\nprovider = "command"\nrun = "limactl shell default"\n')
        with self.assertRaisesRegex(CloudsheepError, '{cmd}'):
            config.machine('x').run_argv(['ls'])

    def test_braces_that_are_not_placeholders_pass_through(self):
        self.config('[machines.x]\nprovider = "command"\nshell = "awk \'{print}\' {nope} {name}"\n')
        self.assertEqual(config.machine('x').shell_argv(), ['sh', '-c', "awk '{print}' {nope} x"])

    def test_unknown_provider(self):
        self.config('[machines.x]\nprovider = "nope"\n')
        with self.assertRaisesRegex(CloudsheepError, 'unknown provider'):
            config.machines()


class GcpWorkerTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.kit = self.tmp / 'kit'
        self.kit.mkdir()
        (self.kit / 'agent.py').write_text(FAKE_AGENT)
        self.leases = self.tmp / 'leases'
        self.leases.mkdir()
        self.agent_log = self.tmp / 'agent.log'
        os.environ['FAKE_AGENT_LOG'] = str(self.agent_log)
        self.provider = f'[providers.gcp-worker]\nkit = "{self.kit}"\nstate_dir = "{self.leases}"\nssh_user = "me_example_com"\n'

    def lease(self, task, **fields):
        record = {'task': task, 'name': 'bundle-agent-0123', 'adopted': False, 'instance_id': '42',
                  'native_termination_time': '2026-10-05T14:00:00Z',
                  'config': {'project': 'proj', 'zone': 'us-central1-b'}, **fields}
        (self.leases / f'lease-{task}.json').write_text(json.dumps(record))

    def calls(self):
        return [json.loads(line) for line in self.agent_log.read_text().splitlines()]

    def test_discovers_unreleased_leases(self):
        self.lease('heavy')
        self.lease('old', released=True)
        self.config(self.provider)
        found = config.machines()
        self.assertEqual(list(found), ['heavy'])
        self.assertIn('bundle-agent-0123', found['heavy'].describe())

    def test_configured_machine_shadows_discovery_and_clash_is_reported(self):
        self.lease('heavy')
        self.config(self.provider + '[machines.big]\nprovider = "gcp-worker"\ntask = "heavy"\n')
        self.assertEqual(list(config.machines()), ['big'])
        self.config(self.provider + '[machines.heavy]\nprovider = "ssh"\nhost = "h"\n')
        with self.assertRaisesRegex(CloudsheepError, 'prefix'):
            config.machines()

    def test_status_and_jobs_normalised(self):
        self.lease('heavy')
        self.config(self.provider)
        status = config.machine('heavy').status()
        self.assertEqual(status['state'], 'RUNNING')
        self.assertEqual(status['hard_deadline'], '2026-10-05T14:00:00Z')
        self.assertEqual(status['jobs'], [{'job': 'j1', 'state': 'running', 'exit_code': None, 'agent': 'codex'}])
        self.assertEqual(self.calls()[0][:3], ['status', '--task', 'heavy'])

    def test_sync_collect_and_refusal_passthrough(self):
        self.lease('heavy')
        self.config(self.provider)
        heavy = config.machine('heavy')
        heavy.sync(Path('/repo'), ['new.py'])
        heavy.collect(Path('/repo'), 'codex/worker/x', [])
        self.assertEqual(self.calls()[0], ['sync', '--task', 'heavy', '--repo', '/repo', '--include-untracked', 'new.py'])
        self.assertEqual(self.calls()[1], ['collect', '--task', 'heavy', '--repo', '/repo', '--branch', 'codex/worker/x'])
        os.environ['FAKE_AGENT_REFUSE'] = '1'
        with self.assertRaisesRegex(CloudsheepError, 'unsaved files'):
            heavy.sync(Path('/repo'), [])

    def test_up_plans_with_dry_run_then_applies(self):
        self.config(self.provider + '[machines.big]\nprovider = "gcp-worker"\ntask = "big"\n'
                    'create = { lease = "1h", ttl = "4h" }\n')
        big = config.machine('big')
        plan = big.up(apply=False)
        self.assertTrue(plan['billable_creation'])
        self.assertIn('--dry-run', self.calls()[0])
        big.up(apply=True)
        self.assertEqual(self.calls()[1], ['acquire', '--task', 'big', '--approve-create', '--lease', '1h', '--ttl', '4h'])

    def test_up_adopt(self):
        self.config(self.provider + '[machines.big]\nprovider = "gcp-worker"\ntask = "big"\nexisting_name = "vm-1"\n')
        config.machine('big').up(apply=True)
        self.assertEqual(self.calls()[0], ['acquire', '--task', 'big', '--existing-name', 'vm-1'])

    def test_down_requires_repo_for_created_workers(self):
        self.lease('heavy')
        self.config(self.provider)
        heavy = config.machine('heavy')
        with self.assertRaisesRegex(CloudsheepError, '--repo'):
            heavy.down(apply=True)
        plan = heavy.down(apply=False, repo=Path('/repo'))
        self.assertIn('DELETES', plan['effect'])
        self.assertFalse(self.agent_log.exists())
        heavy.down(apply=True, repo=Path('/repo'))
        self.assertEqual(self.calls()[0], ['release', '--task', 'heavy', '--repo', '/repo'])

    def test_down_adopted_without_repo(self):
        self.lease('heavy', adopted=True)
        self.config(self.provider)
        heavy = config.machine('heavy')
        self.assertFalse(heavy.down_wants_repo(explicit=False))
        self.assertTrue(heavy.down_wants_repo(explicit=True))
        plan = heavy.down(apply=False)
        self.assertEqual(plan['effect'], 'relinquishes the lease; the VM is kept')
        self.assertNotIn('--repo', plan['would_run'])
        self.assertIn('collects', heavy.down(apply=False, repo=Path('/repo'))['effect'])

    def test_extend_plans_unless_applied(self):
        self.lease('heavy')
        self.config(self.provider)
        heavy = config.machine('heavy')
        self.assertTrue(heavy.down_wants_repo(explicit=False))
        plan = heavy.extend('2h', apply=False)
        self.assertFalse(plan['applied'])
        self.assertIn('2026-10-05T14:00:00Z', plan['effect'])
        self.assertFalse(self.agent_log.exists())
        heavy.extend('2h', apply=True)
        self.assertEqual(self.calls()[0], ['renew', '--task', 'heavy', '--for', '2h'])

    def test_interactive_argv(self):
        self.lease('heavy')
        self.config(self.provider)
        heavy = config.machine('heavy')
        shell = heavy.shell_argv()
        self.assertEqual(shell[:4], ['gcloud', 'compute', 'ssh', 'bundle-agent-0123'])
        self.assertIn('--tunnel-through-iap', shell)
        self.assertIn('--ssh-flag=-oForwardAgent=no', shell)
        self.assertTrue(shell[-1].startswith('--command=sudo -n runuser -l agent -c '))
        self.assertIn('/tasks/heavy/source', shell[-1])
        self.assertIn('--ssh-flag=-L127.0.0.1:16080:127.0.0.1:6080', heavy.tunnel_argv(16080, 6080))
        self.assertEqual(heavy.port_presets()['novnc'], 6080)
        native = heavy.native_argv(['run-agent', '--job', 'x'])
        self.assertEqual(native[-5:], ['run-agent', '--task', 'heavy', '--job', 'x'])

    def test_unbound_lease_refuses_interactive(self):
        self.lease('heavy', instance_id=None)
        self.config(self.provider)
        with self.assertRaisesRegex(CloudsheepError, 'not bound'):
            config.machine('heavy').shell_argv()

    def test_ssh_config_uses_iap_proxy(self):
        self.lease('heavy')
        self.config(self.provider)
        entry = config.machine('heavy').ssh_config()
        self.assertEqual(entry['host'], 'cs-heavy')
        self.assertEqual(entry['options']['User'], 'me_example_com')
        self.assertIn('start-iap-tunnel %h 22 --listen-on-stdin --project=proj --zone=us-central1-b',
                      entry['options']['ProxyCommand'])
        self.assertEqual(entry['options']['HostKeyAlias'], 'compute.42')

    def test_ssh_config_needs_ssh_user_without_calling_gcloud(self):
        self.lease('heavy')
        self.config(self.provider.replace('ssh_user = "me_example_com"\n', ''))
        with self.assertRaisesRegex(CloudsheepError, 'ssh_user'):
            config.machine('heavy').ssh_config()

    def test_jobs_and_logs(self):
        self.lease('heavy')
        self.config(self.provider)
        heavy = config.machine('heavy')
        heavy.submit_job(['make', 'test'], 'j1')
        self.assertEqual(self.calls()[0], ['exec', '--task', 'heavy', '--job', 'j1', '--', 'make', 'test'])
        self.assertEqual(heavy.job_logs('j1', 'stderr', 0), {'text': 'hello', 'next_offset': 5})
        self.assertEqual(heavy.job_status('j1')['state'], 'completed')


if __name__ == '__main__':
    unittest.main()
