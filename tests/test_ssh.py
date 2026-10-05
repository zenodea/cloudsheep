import os
import shlex
import subprocess
import time
import unittest
from pathlib import Path

from cloudsheep import config
from cloudsheep.core import CloudsheepError
from helpers import Sandbox

BOX = '''
[machines.box]
provider = "ssh"
host = "box.example"
user = "dev"
port = 2222
workdir = "~/src/proj"
exclude = ["secrets/"]
ports = { app = 3000 }
up = "echo starting {name} {region}"
region = "eu west"
'''


class SshProviderTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.config(BOX)
        self.box = config.machine('box')

    def test_capabilities_drop_unconfigured_hooks(self):
        caps = self.box.capabilities()
        self.assertIn('up', caps)
        self.assertNotIn('down', caps)
        self.assertTrue({'status', 'shell', 'run', 'tunnel', 'sync', 'collect', 'jobs', 'ssh_config'} <= caps)

    def test_status_reachable_and_unreachable(self):
        self.assertEqual(self.box.status()['state'], 'reachable')
        os.environ['FAKE_SSH_FAIL'] = '1'
        status = self.box.status()
        self.assertEqual(status['state'], 'unreachable')
        self.assertIn('Connection refused', status['error'])

    def test_ssh_argv_and_tunnel(self):
        argv = self.box.run_argv(['echo', 'a b'])
        self.assertEqual(argv[:3], ['ssh', '-p', '2222'])
        self.assertIn('dev@box.example', argv)
        self.assertEqual(argv[-1], 'sh -c ' + shlex.quote("mkdir -p ~/src/proj && cd ~/src/proj && echo 'a b'"))
        tunnel = self.box.tunnel_argv(8080, 3000)
        self.assertIn('127.0.0.1:8080:127.0.0.1:3000', tunnel)
        self.assertIn('-N', tunnel)
        self.assertEqual(self.box.port_presets(), {'app': 3000})

    def test_up_hook_plans_then_applies(self):
        plan = self.box.up(apply=False)
        self.assertFalse(plan['applied'])
        self.assertEqual(plan['would_run'], "echo starting box 'eu west'")
        self.assertEqual(self.box.up(apply=True)['output'], 'starting box eu west')

    def test_sync_then_collect_roundtrip(self):
        repo = self.make_repo()
        (repo / 'app.py').write_text('print("v2 dirty")\n')        # dirty tracked
        (repo / 'notes.md').write_text('untracked\n')               # untracked, not ignored
        (repo / '.env').write_text('TOKEN=secret\n')                # secret-looking
        (repo / 'secrets').mkdir()
        (repo / 'secrets' / 'k.txt').write_text('x\n')              # configured exclude
        (repo / 'build').mkdir()
        (repo / 'build' / 'out.bin').write_text('ignored\n')        # gitignored
        result = self.box.sync(repo, [])
        remote = self.remote_home / 'src' / 'proj'
        self.assertEqual((remote / 'app.py').read_text(), 'print("v2 dirty")\n')
        self.assertTrue((remote / 'notes.md').exists())
        self.assertTrue((remote / '.env.example').exists())
        for absent in ('.env', 'secrets/k.txt', 'build/out.bin', '.git'):
            self.assertFalse((remote / absent).exists(), absent)
        self.assertIn('.env', result['skipped_secret_like'])
        self.assertTrue(result['dirty'])

        # Work happens on the machine.
        (remote / 'app.py').write_text('print("v3 remote")\n')
        (remote / 'new.py').write_text('print("new")\n')
        (remote / 'gone.txt').unlink()
        (remote / 'build').mkdir()
        (remote / 'build' / 'big.o').write_text('artifact\n')
        (remote / '.env').write_text('REMOTE=secret\n')

        head = self.git(repo, 'rev-parse', 'HEAD')
        status_before = self.git(repo, 'status', '--porcelain')
        collected = self.box.collect(repo, 'cloudsheep/test', [])
        self.assertTrue(collected['changed'])
        self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.git(repo, 'status', '--porcelain'), status_before)
        self.assertEqual((repo / 'app.py').read_text(), 'print("v2 dirty")\n')
        files = set(self.git(repo, 'ls-tree', '-r', '--name-only', 'cloudsheep/test').split())
        self.assertIn('new.py', files)
        self.assertIn('notes.md', files)
        self.assertNotIn('gone.txt', files)
        self.assertNotIn('.env', files)
        self.assertNotIn('build/big.o', files)
        self.assertEqual(self.git(repo, 'show', 'cloudsheep/test:app.py'), 'print("v3 remote")\n')
        self.assertEqual(self.git(repo, 'log', '-1', '--format=%ae', 'cloudsheep/test').strip(), 'test@example.com')
        with self.assertRaises(CloudsheepError):
            self.box.collect(repo, 'cloudsheep/test', [])

    def test_collect_with_gitignore_negation_still_sees_changes(self):
        repo = self.make_repo()
        (repo / '.gitignore').write_text('*\n!.gitignore\n!app.py\n')
        self.git(repo, 'add', '-A')
        self.git(repo, 'commit', '-q', '-m', 'allowlist ignore')
        self.box.sync(repo, [])
        (self.remote_home / 'src' / 'proj' / 'app.py').write_text('print("remote")\n')
        collected = self.box.collect(repo, 'cloudsheep/neg', [])
        self.assertTrue(collected['changed'])
        self.assertEqual(self.git(repo, 'show', 'cloudsheep/neg:app.py'), 'print("remote")\n')

    def test_collect_ignores_global_hooks_and_signing(self):
        repo = self.make_repo()
        hooks = self.tmp / 'hooks'
        hooks.mkdir()
        (hooks / 'pre-commit').write_text('#!/bin/sh\nexit 1\n')
        (hooks / 'pre-commit').chmod(0o755)
        Path(os.environ['GIT_CONFIG_GLOBAL']).write_text(f'[core]\n\thooksPath = {hooks}\n[commit]\n\tgpgsign = true\n')
        self.box.sync(repo, [])
        (self.remote_home / 'src' / 'proj' / 'new.py').write_text('x\n')
        self.assertTrue(self.box.collect(repo, None, [])['changed'])

    def test_collect_without_sync_refuses(self):
        repo = self.make_repo()
        with self.assertRaisesRegex(CloudsheepError, 'sync first'):
            self.box.collect(repo, None, [])

    def test_collect_without_changes(self):
        repo = self.make_repo()
        self.box.sync(repo, [])
        self.assertFalse(self.box.collect(repo, None, [])['changed'])

    def wait(self, job, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.box.job_status(job)
            if status['state'] != 'running':
                return status
            time.sleep(0.1)
        self.fail('job did not finish')

    def test_detached_job_lifecycle(self):
        (self.remote_home / 'src' / 'proj').mkdir(parents=True)
        submitted = self.box.submit_job(['sh', '-c', 'pwd; echo out; echo err >&2; exit 3'], 'build-1')
        self.assertEqual(submitted['state'], 'submitted')
        status = self.wait('build-1')
        self.assertEqual((status['state'], status['exit_code']), ('completed', 3))
        logs = self.box.job_logs('build-1', 'stdout', 0)
        self.assertTrue(logs['text'].endswith('src/proj\nout\n'), logs['text'])
        tail = self.box.job_logs('build-1', 'stdout', logs['next_offset'] - 4)
        self.assertEqual(tail['text'], 'out\n')
        self.assertEqual(self.box.job_logs('build-1', 'stderr', 0)['text'], 'err\n')
        with self.assertRaises(CloudsheepError):
            self.box.submit_job(['true'], 'build-1')
        self.assertEqual([j['job'] for j in self.box.status()['jobs']], ['build-1'])

    def test_cancel_job(self):
        (self.remote_home / 'src' / 'proj').mkdir(parents=True)
        self.box.submit_job(['sleep', '30'], 'slow')
        time.sleep(0.3)
        self.assertEqual(self.box.job_status('slow')['state'], 'running')
        self.box.job_cancel('slow')
        status = self.wait('slow')
        self.assertIn(status['state'], {'completed', 'interrupted'})
        self.assertNotEqual(status['exit_code'], 0)

    def test_logs_for_missing_job(self):
        with self.assertRaisesRegex(CloudsheepError, 'no job ghost'):
            self.box.job_logs('ghost', 'stdout', 0)

    def test_scripts_survive_a_non_posix_login_shell(self):
        # The fake ssh runs the remote command with /bin/sh; csh-style syntax would fail if unwrapped.
        self.assertTrue(self.box.run_argv(['true'])[-1].startswith('sh -c '))
        self.assertTrue(self.box.shell_argv()[-1].startswith('sh -c '))

    def test_invalid_job_id(self):
        with self.assertRaises(CloudsheepError):
            self.box.submit_job(['true'], 'Bad;id')

    def test_ssh_config(self):
        entry = self.box.ssh_config()
        self.assertEqual(entry['host'], 'box')
        self.assertEqual(entry['options']['Port'], '2222')
        self.assertEqual(entry['options']['ForwardAgent'], 'no')


    def test_proxy_command_placeholders(self):
        self.config('[machines.g]\nprovider = "ssh"\nhost = "vm-1"\nzone = "us-central1-c"\n'
                    'proxy_command = "gcloud compute start-iap-tunnel %h 22 --listen-on-stdin --zone={zone}"\n')
        machine = config.machine('g')
        argv = machine.ssh_base()
        self.assertIn('ProxyCommand=gcloud compute start-iap-tunnel %h 22 --listen-on-stdin --zone=us-central1-c', argv)
        self.assertEqual(machine.ssh_config()['options']['ProxyCommand'],
                         'gcloud compute start-iap-tunnel %h 22 --listen-on-stdin --zone=us-central1-c')

if __name__ == '__main__':
    unittest.main()
