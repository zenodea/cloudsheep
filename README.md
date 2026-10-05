# cloudsheep 🐑

Herd your VMs from the terminal and from [herdr](https://herdr.dev): shells, port tunnels,
source sync, detached jobs and lifecycle, behind one CLI, whatever the VM runs on.

```
cloudsheep ls                          # machines from ~/.config/cloudsheep/machines.toml
cloudsheep shell box                   # shell in the machine's workdir
cloudsheep sync box                    # send the current repo (tracked + nonignored files)
cloudsheep job box --job build -- make -j16
cloudsheep logs box build --follow
cloudsheep collect box                 # bring changes back as a new local branch
cloudsheep tunnel box 3000             # or 9000:3000, or a preset like `app` / `novnc`
cloudsheep up box / down box           # prints the plan; add --yes to apply
cloudsheep watch                       # herdr notifications: job done, lease expiring
```

Python 3.11+ standard library only; `rsync` and `ssh` for the ssh provider, `fzf` and `jq` for the herdr picker.

## Install

```sh
ln -s ~/Work/cloudsheep/bin/cloudsheep ~/.local/bin/cloudsheep
cloudsheep init          # writes ~/.config/cloudsheep/machines.toml from examples/
```

## Providers

Each machine names a provider. A provider supports a set of operations; `cloudsheep caps NAME`
lists them and unsupported ones fail cleanly.

| provider | for | supports |
|---|---|---|
| `ssh` | anything you can ssh into (cloud VMs, a box under the desk, Multipass/Lima/Tart guests) | status, shell, run, tunnel, sync, collect, jobs, ssh-config; up/down/extend through optional local hook commands |
| `command` | VM tools with a CLI (limactl, multipass, orb, tart, aws ssm, ...) | whatever you give a shell template for |
| `gcp-worker` | bundle's disposable GCP workers (`scripts/gcp-worker/agent.py`) | everything, plus `native` passthrough (e.g. `run-agent`) |

See [`examples/machines.toml`](examples/machines.toml) for each.

Templates (the `command` provider and the ssh lifecycle hooks) fill `{name}`, any string setting such as
`{instance}`, and per-operation values `{cmd}`, `{local_port}`, `{remote_port}`, `{remote_host}`, `{repo}`,
`{branch}`, `{duration}`. Values are shell-quoted. Other braces (JSON, awk, jq) are left alone.

### ssh: sync, collect and jobs

- **sync** rsyncs tracked and nonignored untracked files, including uncommitted edits, into `workdir`.
  It never deletes remote files. Paths that look like secrets (`.env*` except `.env.example`, keys, `.ssh/`,
  `.aws/`, ...) and your `exclude` list are skipped and reported. This is not a secret scanner.
- **collect** copies `workdir` into a throwaway clone at the commit you last synced, commits the
  difference and fetches it as `cloudsheep/<machine>-<time>` (or `--branch`). Your HEAD, index and
  working tree are untouched. Files ignored by the root `.gitignore` are not transferred back.
- **jobs** run detached under `~/.cloudsheep/jobs/<id>` on the machine (`setsid`/`nohup`), so they
  survive disconnects. `logs --follow` polls until the job ends. `cancel` signals its process group.

### gcp-worker

cloudsheep only calls `agent.py`. Leases, identity checks, the cap, sync/collect and deletion stay
with agent.py, and its refusals are shown as they are. With a `[providers.gcp-worker]` table, every
unreleased lease shows up as a machine named after its task. To acquire one, describe it as a machine
(`existing_name` to adopt, or a `create` table) and run `cloudsheep up NAME`. Without `--yes` it runs
agent.py's `--dry-run` and shows the plan, including whether creation is billable.

- `shell` and `run` use `gcloud compute ssh --tunnel-through-iap`, switch to the `agent` user and start in
  the task's synced source. Interactive edits are invisible to agent.py's job tracking: leave the
  shell before `collect` or `down`.
- `tunnel novnc` forwards the worker desktop (`http://127.0.0.1:6080/vnc.html`).
- `down` on a created worker collects and then **deletes** the VM (it needs a repo). On an adopted
  worker it only gives up the lease.
- `ssh-config` emits an IAP `ProxyCommand` block (`cs-<task>`), so plain `ssh`, `rsync` and
  `herdr --remote` can reach the VM.
- `native NAME -- run-agent --job ... --agent codex ...` passes any other agent.py command through.

## herdr

`herdr/keys.toml` has the bindings. Paste them into `~/.config/herdr/config.toml` and reload:

- **prefix+u** opens a popup to pick a machine, then an action. Shells open in a new tab named after the
  machine. Logs and tunnels open in a split. Sync and collect use the focused pane's repo. up and down
  show the plan and ask before applying.
- **prefix+shift+u** toggles a background `cloudsheep watch`, which sends herdr notifications when
  jobs finish or a lease or hard deadline is within 20 minutes.

`cloudsheep open NAME [--split right|down] [-- ACTION ARGS]` is the building block for your own
bindings. It opens a herdr tab or split and runs `cloudsheep ACTION NAME ARGS` in it.

## Tests

```sh
PYTHONPATH=.:tests python3 -m unittest discover -s tests
```

Tests use a fake `ssh` that runs remote commands locally, a fake `herdr` and a fake `agent.py`.
Real rsync and git exercise sync and collect, and real detached jobs run. No cloud calls are made.
