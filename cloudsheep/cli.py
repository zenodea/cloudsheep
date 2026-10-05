"""cloudsheep: herd your VMs from the terminal (and from herdr)."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, gitsync
from .core import CloudsheepError, run, run_json

ROOT = Path(__file__).resolve().parent.parent


# Output ------------------------------------------------------------------------
def show(value, indent=0):
    pad = ' ' * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if item in (None, '', [], {}):
                continue
            if isinstance(item, (dict, list)):
                print(f'{pad}{key}:')
                show(item, indent + 2)
            elif isinstance(item, str) and '\n' in item:
                print(f'{pad}{key}:')
                for line in item.splitlines():
                    print(f'{pad}  {line}')
            else:
                print(f'{pad}{key}: {item}')
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                print(pad + '- ' + '  '.join(f'{k}={v}' for k, v in item.items() if v not in (None, '')))
            else:
                print(f'{pad}- {item}')
    else:
        print(f'{pad}{value}')


def emit(args, value):
    if getattr(args, 'json', False):
        print(json.dumps(value, indent=2))
    else:
        show(value)


def exec_argv(argv, notice=None):
    if notice:
        print(f'cloudsheep: {notice}', file=sys.stderr)
    sys.stdout.flush()
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError:
        raise CloudsheepError(f'command not found: {argv[0]}') from None


def self_command() -> list[str]:
    if os.environ.get('CLOUDSHEEP_BIN'):
        return shlex.split(os.environ['CLOUDSHEEP_BIN'])
    return [str(ROOT / 'bin' / 'cloudsheep')]


def repo_for(args, machine) -> Path:
    chosen = args.repo or machine.settings.get('repo') or machine.provider_settings.get('repo')
    return gitsync.toplevel(Path(chosen).expanduser() if chosen else Path.cwd())


def parse_tunnel(spec: str, presets: dict) -> tuple[int, int]:
    name, _, local = spec.partition(':')
    if name in presets:
        remote = presets[name]
        return (int(local) if local else remote), remote
    match = re.fullmatch(r'(\d+)(?::(\d+))?', spec)
    if not match:
        known = ', '.join(presets) or 'none'
        raise CloudsheepError(f'tunnel spec is PORT, LOCAL:REMOTE or a preset[:LOCAL] (presets: {known})')
    local_port, remote_port = int(match[1]), int(match[2] or match[1])
    return local_port, remote_port


def parse_duration(text: str) -> timedelta:
    match = re.fullmatch(r'(\d+)([smhd])', text)
    if not match:
        raise argparse.ArgumentTypeError('use a duration like 90s, 15m, 2h or 1d')
    return timedelta(seconds=int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]])


# Commands --------------------------------------------------------------------------
def cmd_ls(args):
    found = config.machines()
    rows = []
    for name, machine in sorted(found.items()):
        row = {'name': name, 'provider': machine.provider, 'description': machine.describe(),
               'capabilities': sorted(machine.capabilities()), 'ports': machine.port_presets()}
        if args.status and 'status' in machine.capabilities():
            try:
                status = machine.status()
                row['state'] = status.get('state', '')
                row['expires_at'] = status.get('expires_at', '')
            except CloudsheepError as error:
                row['state'] = f'error: {error}'
        rows.append(row)
    if args.json:
        print(json.dumps(rows, indent=2))
    elif args.plain:
        for row in rows:
            print('\t'.join(str(row.get(k, '')) for k in ('name', 'provider', 'state', 'description')))
    elif not rows:
        print(f'no machines; add some to {config.config_path()} (see `cloudsheep init`)')
    else:
        width = max(len(r['name']) for r in rows)
        for row in rows:
            state = f"  [{row['state']}]" if row.get('state') else ''
            print(f"{row['name']:<{width}}  {row['provider']:<10}  {row['description']}{state}")


def cmd_caps(args):
    machine = config.machine(args.name)
    for capability in sorted(machine.capabilities()):
        print(capability)
    if args.ports:
        for preset, port in machine.port_presets().items():
            print(f'port:{preset}:{port}')


def cmd_status(args):
    machine = config.machine(args.name)
    machine.require('status')
    emit(args, machine.status())


def cmd_shell(args):
    machine = config.machine(args.name)
    machine.require('shell')
    exec_argv(machine.shell_argv(as_self=args.as_self), None if args.as_self else machine.shell_notice())


def cmd_run(args):
    machine = config.machine(args.name)
    machine.require('run')
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        raise CloudsheepError('run needs a command after --')
    exec_argv(machine.run_argv(command, tty=args.tty))


def cmd_tunnel(args):
    machine = config.machine(args.name)
    machine.require('tunnel')
    local, remote = parse_tunnel(args.spec, machine.port_presets())
    print(f'cloudsheep: http://127.0.0.1:{local} -> {args.name}:{remote} (Ctrl-C to close)', file=sys.stderr)
    exec_argv(machine.tunnel_argv(local, remote, args.remote_host))


def cmd_sync(args):
    machine = config.machine(args.name)
    machine.require('sync')
    emit(args, machine.sync(repo_for(args, machine), args.include))


def cmd_collect(args):
    machine = config.machine(args.name)
    machine.require('collect')
    emit(args, machine.collect(repo_for(args, machine), args.branch, args.include))


def planned(args):
    if not args.yes and not args.json:
        sys.stdout.flush()
        print('\nnothing done; rerun with --yes to apply', file=sys.stderr)


def cmd_up(args):
    machine = config.machine(args.name)
    machine.require('up')
    emit(args, machine.up(apply=args.yes))
    planned(args)


def cmd_down(args):
    machine = config.machine(args.name)
    machine.require('down')
    wants = not args.no_repo and machine.down_wants_repo(explicit=bool(args.repo))
    repo = repo_for(args, machine) if wants else None
    emit(args, machine.down(apply=args.yes, repo=repo, branch=args.branch))
    planned(args)


def cmd_extend(args):
    machine = config.machine(args.name)
    machine.require('extend')
    emit(args, machine.extend(args.duration, apply=args.yes))
    planned(args)


def cmd_job(args):
    machine = config.machine(args.name)
    machine.require('jobs')
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        raise CloudsheepError('job needs a command after --')
    emit(args, machine.submit_job(command, args.job or 'job-' + uuid.uuid4().hex[:10]))


def cmd_jobs(args):
    machine = config.machine(args.name)
    machine.require('jobs')
    status = machine.status()
    emit(args, status.get('jobs', []))


def cmd_cancel(args):
    machine = config.machine(args.name)
    machine.require('jobs')
    emit(args, machine.job_cancel(args.job))


ACTIVE = {'running', 'submitted', 'activating', 'unknown'}


def cmd_logs(args):
    machine = config.machine(args.name)
    machine.require('jobs')
    offset = args.offset
    if args.json:
        chunk = machine.job_logs(args.job, args.stream, offset, args.limit)
        print(json.dumps({'machine': machine.name, 'job': args.job, 'stream': args.stream, 'offset': offset, **chunk}))
        return
    while True:
        chunk = machine.job_logs(args.job, args.stream, offset)
        if chunk['text']:
            sys.stdout.write(chunk['text'])
            sys.stdout.flush()
        offset = chunk['next_offset']
        if not args.follow:
            return
        if not chunk['text']:
            status = machine.job_status(args.job)
            if status.get('state') not in ACTIVE:
                tail = machine.job_logs(args.job, args.stream, offset)
                sys.stdout.write(tail['text'])
                print(f"\ncloudsheep: job {args.job} {status.get('state')} (exit {status.get('exit_code')})",
                      file=sys.stderr)
                return
            time.sleep(args.interval)


def format_ssh_config(entry: dict) -> str:
    lines = [f"Host {entry['host']}"]
    lines += [f'  {key} {value}' for key, value in entry['options'].items()]
    return '\n'.join(lines)


def cmd_ssh_config(args):
    found = config.machines()
    names = args.names or sorted(n for n, m in found.items() if 'ssh_config' in m.capabilities())
    blocks = []
    for name in names:
        machine = config.machine(name)
        machine.require('ssh_config')
        try:
            blocks.append(f'# cloudsheep: {name} ({machine.provider})\n' + format_ssh_config(machine.ssh_config()))
        except CloudsheepError as error:
            if args.names:
                raise
            blocks.append(f'# cloudsheep: {name} skipped: {error}')
    print('\n\n'.join(blocks))


def cmd_native(args):
    machine = config.machine(args.name)
    machine.require('native')
    command = args.args[1:] if args.args[:1] == ['--'] else args.args
    exec_argv(machine.native_argv(command))


# Watch -----------------------------------------------------------------------------
def notifier(mode):
    herdr = os.environ.get('HERDR_BIN_PATH') or shutil.which('herdr')
    use_herdr = mode == 'herdr' or (mode == 'auto' and herdr)

    def notify(title, body, sound='done'):
        stamp = time.strftime('%H:%M:%S')
        print(f'[{stamp}] {title}: {body}', flush=True)
        if use_herdr:
            run([herdr, 'notification', 'show', title, '--body', body, '--sound', sound], check=False)
    return notify


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def cmd_watch(args):
    notify = notifier(args.notify)
    seen: dict[tuple, str] = {}
    warned: set = set()
    first = True
    while True:
        for name, machine in sorted(config.machines().items()):
            if args.names and name not in args.names or 'status' not in machine.capabilities():
                continue
            try:
                status = machine.status()
            except CloudsheepError as error:
                print(f'[{time.strftime("%H:%M:%S")}] {name}: status failed: {error}', flush=True)
                continue
            for job in status.get('jobs') or []:
                key = (name, job.get('job'))
                state = job.get('state')
                if not first and seen.get(key) != state and state not in ACTIVE:
                    code = job.get('exit_code')
                    sound = 'done' if state == 'completed' and code in (0, None) else 'request'
                    notify(f'🐑 {name}', f"job {job.get('job')} {state} (exit {code})", sound)
                seen[key] = state
            for field in ('expires_at', 'hard_deadline'):
                deadline = parse_time(status.get(field)) if status.get(field) else None
                if deadline is None:
                    continue
                left = deadline - datetime.now(timezone.utc)
                if left <= args.warn and (name, field, status.get(field)) not in warned:
                    warned.add((name, field, status.get(field)))
                    minutes = max(0, int(left.total_seconds() // 60))
                    label = 'lease' if field == 'expires_at' else 'hard deadline'
                    notify(f'🐑 {name}', f'{label} ends in {minutes} min ({status.get(field)})', 'request')
        first = False
        if args.once:
            return
        time.sleep(args.interval.total_seconds())


# herdr -----------------------------------------------------------------------------
def herdr_bin():
    herdr = os.environ.get('HERDR_BIN_PATH') or shutil.which('herdr')
    if not herdr:
        raise CloudsheepError('herdr not found')
    return herdr


def pane_id(reply) -> str:
    result = reply.get('result', reply) if isinstance(reply, dict) else {}
    for key in ('root_pane', 'pane'):
        if isinstance(result.get(key), dict) and result[key].get('pane_id'):
            return result[key]['pane_id']
    raise CloudsheepError('herdr did not report the new pane id')


def cmd_open(args):
    """Open a herdr tab or split running another cloudsheep command for a machine."""
    machine = config.machine(args.name)
    action = args.action[1:] if args.action[:1] == ['--'] else args.action
    if not action:
        action = ['shell']
    herdr = herdr_bin()
    if args.split:
        argv = [herdr, 'pane', 'split', '--direction', args.split, '--focus']
        if os.environ.get('HERDR_PANE_ID'):
            argv += ['--pane', os.environ['HERDR_PANE_ID']]
        if args.cwd:
            argv += ['--cwd', args.cwd]
    else:
        argv = [herdr, 'tab', 'create', '--label', f'🐑 {machine.name}', '--focus']
        if args.cwd:
            argv += ['--cwd', args.cwd]
    for key in ('CLOUDSHEEP_CONFIG', 'CLOUDSHEEP_STATE_DIR', 'CLOUDSHEEP_BIN'):
        if os.environ.get(key):
            argv += ['--env', f'{key}={os.environ[key]}']
    pane = pane_id(run_json(argv))
    command = shlex.join([*self_command(), action[0], machine.name, *action[1:]])
    run([herdr, 'pane', 'run', pane, command])
    print(json.dumps({'pane_id': pane, 'command': command}))


EXAMPLE = ROOT / 'examples' / 'machines.toml'


def cmd_init(args):
    path = config.config_path()
    if path.exists():
        print(f'{path} already exists')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE, path)
    print(f'wrote {path}; edit it to add your machines')


# Parser ----------------------------------------------------------------------------
def parser():
    top = argparse.ArgumentParser(prog='cloudsheep', description=__doc__)
    sub = top.add_subparsers(dest='command', required=True, metavar='COMMAND')

    def add(name, func, help, *, name_arg=True, json_flag=True):
        command = sub.add_parser(name, help=help, description=help)
        command.set_defaults(func=func)
        if name_arg:
            command.add_argument('name', help='machine name (see `cloudsheep ls`)')
        if json_flag:
            command.add_argument('--json', action='store_true', help='print JSON')
        return command

    c = add('ls', cmd_ls, 'list machines', name_arg=False)
    c.add_argument('--status', action='store_true', help='also query each machine (slow)')
    c.add_argument('--plain', action='store_true', help='tab-separated output for scripts')
    c = add('caps', cmd_caps, 'list what a machine supports', json_flag=False)
    c.add_argument('--ports', action='store_true', help='also list tunnel port presets')
    add('status', cmd_status, 'show machine state, lease and jobs')
    c = add('shell', cmd_shell, 'open an interactive shell in the machine workdir', json_flag=False)
    c.add_argument('--self', dest='as_self', action='store_true', help='plain login as yourself, no workdir/user switch')
    c = add('run', cmd_run, 'run a command attached, in the machine workdir', json_flag=False)
    c.add_argument('--tty', action='store_true')
    c.add_argument('command', nargs='*', help='command, after --')
    c = add('tunnel', cmd_tunnel, 'forward a local port to the machine', json_flag=False)
    c.add_argument('spec', help='PORT, LOCAL:REMOTE or a preset such as novnc[:LOCAL]')
    c.add_argument('--remote-host', default='127.0.0.1')
    for name, func, help in (('sync', cmd_sync, 'send local source to the machine'),
                             ('collect', cmd_collect, 'bring machine changes back as a new local branch')):
        c = add(name, func, help)
        c.add_argument('--repo', help='local repository (default: machine setting or current directory)')
        c.add_argument('--include', action='append', default=[], help='extra untracked path (gcp-worker)')
        if name == 'collect':
            c.add_argument('--branch', help='new branch name')
    c = add('up', cmd_up, 'start/acquire the machine (shows the plan unless --yes)')
    c.add_argument('--yes', action='store_true', help='actually do it')
    c = add('down', cmd_down, 'stop/release the machine (shows the plan unless --yes)')
    c.add_argument('--yes', action='store_true', help='actually do it')
    c.add_argument('--repo', help='collect into this repository first (gcp-worker; required for created workers)')
    c.add_argument('--no-repo', action='store_true', help='never collect (refused for created gcp workers)')
    c.add_argument('--branch')
    c = add('extend', cmd_extend, 'extend the machine lease (shows the plan unless --yes)')
    c.add_argument('duration', help='e.g. 2h')
    c.add_argument('--yes', action='store_true', help='actually do it')
    c = add('job', cmd_job, 'start a detached job that survives disconnects')
    c.add_argument('--job', help='job id (lowercase, digits, hyphens)')
    c.add_argument('command', nargs='*', help='command, after --')
    add('jobs', cmd_jobs, 'list jobs')
    c = add('logs', cmd_logs, 'print job output (--json: one chunk with next_offset)')
    c.add_argument('job')
    c.add_argument('--limit', type=int, default=65536, help='max bytes per chunk (--json)')
    c.add_argument('--stream', choices=['stdout', 'stderr'], default='stdout')
    c.add_argument('--offset', type=int, default=0)
    c.add_argument('-f', '--follow', action='store_true', help='keep printing until the job ends')
    c.add_argument('--interval', type=float, default=3.0)
    c = add('cancel', cmd_cancel, 'cancel a job')
    c.add_argument('job')
    c = add('ssh-config', cmd_ssh_config, 'print ssh_config Host blocks', name_arg=False, json_flag=False)
    c.add_argument('names', nargs='*')
    c = add('native', cmd_native, "run the provider's own CLI for this machine", json_flag=False)
    c.add_argument('args', nargs='*', help='provider CLI arguments, after --')
    c = add('watch', cmd_watch, 'notify on job completion and lease expiry', name_arg=False, json_flag=False)
    c.add_argument('names', nargs='*')
    c.add_argument('--interval', type=parse_duration, default=timedelta(minutes=2))
    c.add_argument('--warn', type=parse_duration, default=timedelta(minutes=20), help='warn this long before expiry')
    c.add_argument('--notify', choices=['auto', 'herdr', 'print'], default='auto')
    c.add_argument('--once', action='store_true')
    c = add('open', cmd_open, 'open a herdr tab/split running a cloudsheep command', json_flag=False)
    c.add_argument('--split', choices=['right', 'down'], help='split the current pane instead of a new tab')
    c.add_argument('--cwd')
    c.add_argument('action', nargs='*', help='cloudsheep command and its args after -- (default: shell)')
    add('init', cmd_init, 'write an example config if none exists', name_arg=False, json_flag=False)
    return top


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        args.func(args)
    except CloudsheepError as error:
        if getattr(args, 'json', False):
            print(json.dumps({'ok': False, 'error': str(error)}))
        else:
            print(f'cloudsheep: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
