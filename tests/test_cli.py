import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from cloudsheep import cli
from cloudsheep.core import CloudsheepError
from helpers import FAKES, Sandbox


def call(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTest(Sandbox):
    def setUp(self):
        super().setUp()
        soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        self.config(f'''
[machines.box]
provider = "ssh"
host = "box.example"
ports = {{ app = 3000 }}

down = "echo stopping {{name}}"
extend = "echo extend {{duration}}"

[machines.lima]
provider = "command"
status = "printf '{{\\"state\\": \\"Running\\", \\"expires_at\\": \\"{soon}\\"}}'"
shell = "limactl shell default"
''')
        self.herdr_log = self.tmp / 'herdr.log'
        os.environ['FAKE_HERDR_LOG'] = str(self.herdr_log)

    def test_ls_and_caps(self):
        code, out, _ = call('ls', '--plain')
        self.assertEqual(code, 0)
        self.assertEqual([line.split('\t')[0] for line in out.splitlines()], ['box', 'lima'])
        code, out, _ = call('caps', 'lima', '--ports')
        self.assertEqual(out.split(), ['shell', 'status'])
        _, out, _ = call('caps', 'box', '--ports')
        self.assertIn('port:app:3000', out.split())

    def test_ls_json_carries_capabilities_and_ports(self):
        _, out, _ = call('ls', '--json')
        rows = {row['name']: row for row in json.loads(out)}
        self.assertEqual(rows['lima']['capabilities'], ['shell', 'status'])
        self.assertEqual(rows['box']['ports'], {'app': 3000})

    def test_logs_json_chunk(self):
        jobs = self.remote_home / '.cloudsheep' / 'jobs' / 'b1'
        jobs.mkdir(parents=True)
        (jobs / 'stdout').write_text('hello world')
        code, out, err = call('logs', 'box', 'b1', '--json', '--offset', '6', '--limit', '3')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {'machine': 'box', 'job': 'b1', 'stream': 'stdout', 'offset': 6,
                                           'text': 'wor', 'next_offset': 9})

    def test_unsupported_and_unknown_are_clean_errors(self):
        code, _, err = call('sync', 'lima')
        self.assertEqual(code, 1)
        self.assertIn('does not support sync', err)
        code, out, _ = call('status', 'ghost', '--json')
        self.assertEqual(json.loads(out)['ok'], False)

    def test_tunnel_spec(self):
        presets = {'novnc': 6080}
        self.assertEqual(cli.parse_tunnel('8080', presets), (8080, 8080))
        self.assertEqual(cli.parse_tunnel('9000:3000', presets), (9000, 3000))
        self.assertEqual(cli.parse_tunnel('novnc', presets), (6080, 6080))
        self.assertEqual(cli.parse_tunnel('novnc:16080', presets), (16080, 6080))
        with self.assertRaises(CloudsheepError):
            cli.parse_tunnel('nope', presets)

    def herdr_calls(self):
        return [json.loads(line) for line in self.herdr_log.read_text().splitlines()]

    def test_open_tab_runs_cloudsheep_in_new_pane(self):
        os.environ['HERDR_BIN_PATH'] = str(FAKES / 'herdr')
        os.environ['CLOUDSHEEP_BIN'] = '/opt/cs/bin/cloudsheep'
        code, out, err = call('open', 'box')
        self.assertEqual(code, 0, err)
        calls = self.herdr_calls()
        self.assertEqual(calls[0][:4], ['tab', 'create', '--label', '🐑 box'])
        self.assertEqual(calls[1], ['pane', 'run', 'w:p9', '/opt/cs/bin/cloudsheep shell box'])

    def test_open_split_with_action(self):
        os.environ['HERDR_BIN_PATH'] = str(FAKES / 'herdr')
        os.environ['HERDR_PANE_ID'] = 'w:p1'
        os.environ['CLOUDSHEEP_BIN'] = 'cs'
        call('open', 'box', '--split', 'down', '--', 'logs', 'build-1', '--follow')
        calls = self.herdr_calls()
        self.assertEqual(calls[0][:7], ['pane', 'split', '--direction', 'down', '--focus', '--pane', 'w:p1'])
        self.assertIn(f"CLOUDSHEEP_CONFIG={os.environ['CLOUDSHEEP_CONFIG']}", calls[0])
        self.assertEqual(calls[1], ['pane', 'run', 'w:p7', 'cs logs box build-1 --follow'])

    def test_watch_warns_once_about_expiry(self):
        os.environ['HERDR_BIN_PATH'] = str(FAKES / 'herdr')
        code, out, err = call('watch', 'lima', '--once', '--notify', 'herdr')
        self.assertEqual(code, 0, err)
        self.assertIn('lease ends in', out)
        notifications = [c for c in self.herdr_calls() if c[:2] == ['notification', 'show']]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0][2], '🐑 lima')

    def test_run_and_job_take_command_after_double_dash(self):
        args = cli.parser().parse_args(['run', 'box', '--tty', '--', 'ls', '-la'])
        self.assertEqual((args.tty, args.command), (True, ['ls', '-la']))
        args = cli.parser().parse_args(['job', 'box', '--job', 'b1', '--', 'make', '-j8'])
        self.assertEqual((args.job, args.command), ('b1', ['make', '-j8']))

    def test_down_on_hook_machine_ignores_non_repo_cwd(self):
        code, out, err = call('down', 'box', '--repo', str(self.tmp), '--yes')
        self.assertEqual(code, 0, err)
        self.assertIn('stopping box', out)

    def test_extend_needs_yes(self):
        code, out, err = call('extend', 'box', '2h')
        self.assertIn("would_run: echo extend 2h", out)
        self.assertIn('rerun with --yes', err)
        _, out, _ = call('extend', 'box', '2h', '--yes')
        self.assertIn('output: extend 2h', out)

    def test_up_without_hook_is_unsupported(self):
        code, _, err = call('up', 'box')
        self.assertEqual(code, 1)
        self.assertIn('does not support up', err)

    def test_ssh_config_lists_capable_machines(self):
        code, out, _ = call('ssh-config')
        self.assertEqual(code, 0)
        self.assertIn('Host box\n  HostName box.example', out)
        self.assertNotIn('lima', out)


if __name__ == '__main__':
    unittest.main()
