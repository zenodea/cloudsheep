#!/usr/bin/env bash
# herdr popup: pick a machine, then an action. Bind it with herdr/keys.toml.
# Usage: cloudsheep-pick.sh [MACHINE]
set -uo pipefail

here="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
cs="${CLOUDSHEEP_BIN:-$here/../bin/cloudsheep}"
export CLOUDSHEEP_BIN="$cs"
cwd="${HERDR_ACTIVE_PANE_CWD:-$PWD}"

fzf_opts=(--no-info --reverse --delimiter=$'\t'
  --color="bg:-1,bg+:-1,fg:#f8f8f2,fg+:#ffffff,hl:#bd93f9,hl+:#bd93f9,prompt:#bd93f9,pointer:#bd93f9")

pause() { printf '\n\033[2m(any key to close)\033[0m'; read -rsn1 _ || true; }
fail() { printf '\n%s\n' "$*"; pause; exit 1; }
confirm() { local reply; read -rp "$1 [y/N] " reply; [[ $reply == [yY]* ]]; }

machine="${1:-}"
if [[ -z $machine ]]; then
  list="$("$cs" ls --plain)" || fail "could not list machines"
  [[ -n $list ]] || fail "no machines yet: run '$cs init' and edit ~/.config/cloudsheep/machines.toml"
  machine="$(awk -F'\t' '{printf "%s\t%-10s\t%s\n", $1, $2, $4}' <<<"$list" |
    fzf "${fzf_opts[@]}" --prompt='🐑 machine › ' | cut -f1)"
  [[ -n $machine ]] || exit 0
fi

caps="$("$cs" caps --ports "$machine")" || fail "unknown machine $machine"
has() { grep -qx "$1" <<<"$caps"; }
repo_name="$(basename "$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || echo "$cwd")")"

menu=()
has shell && menu+=($'shell\topen a shell in a new tab')
has status && menu+=($'status\tstate, lease and jobs')
has jobs && menu+=($'logs\tfollow a job in a split') && menu+=($'job\tstart a detached job')
has tunnel && menu+=($'tunnel\tforward a port in a split')
has sync && menu+=("sync"$'\t'"send $repo_name to the machine")
has collect && menu+=($'collect\tbring changes back as a new branch')
has extend && menu+=($'extend\textend the lease')
has up && menu+=($'up\tstart / acquire')
has down && menu+=($'down\tstop / release')
has ssh_config && menu+=($'ssh-config\tprint an ssh_config entry')
[[ ${#menu[@]} -gt 0 ]] || fail "$machine supports no actions"

action="$(printf '%s\n' "${menu[@]}" | fzf "${fzf_opts[@]}" --prompt="🐑 $machine › " | cut -f1)"

case "$action" in
  shell)
    "$cs" open "$machine" -- shell >/dev/null || fail "could not open a pane" ;;
  status)
    "$cs" status "$machine"; pause ;;
  logs)
    job="$("$cs" jobs "$machine" --json | jq -r '.[] | "\(.job)\t\(.state)\t\(.exit_code // "")"' |
      fzf "${fzf_opts[@]}" --prompt="🐑 $machine job › " | cut -f1)"
    [[ -n $job ]] && { "$cs" open "$machine" --split down -- logs "$job" --follow >/dev/null || fail "could not open a pane"; } ;;
  job)
    read -rp "command (runs with sh -c in the workdir): " cmd
    read -rp "job id (blank for random): " job
    [[ -n $cmd ]] && "$cs" job "$machine" ${job:+--job "$job"} -- sh -c "$cmd"
    pause ;;
  tunnel)
    presets="$(sed -n 's/^port:\([^:]*\):\(.*\)$/\1=\2/p' <<<"$caps" | paste -sd' ' -)"
    read -rp "port, LOCAL:REMOTE or preset${presets:+ ($presets)}: " spec
    [[ -n $spec ]] && { "$cs" open "$machine" --split down -- tunnel "$spec" >/dev/null || fail "could not open a pane"; } ;;
  sync)
    "$cs" sync "$machine" --repo "$cwd"; pause ;;
  collect)
    "$cs" collect "$machine" --repo "$cwd"; pause ;;
  extend)
    read -rp "extend for (e.g. 2h): " duration
    [[ -n $duration ]] && "$cs" extend "$machine" "$duration"
    pause ;;
  up)
    "$cs" up "$machine" || fail "plan failed"
    echo; confirm "Apply this?" && "$cs" up "$machine" --yes
    pause ;;
  down)
    "$cs" down "$machine" --repo "$cwd" || fail "plan failed"
    echo; confirm "Apply this?" && "$cs" down "$machine" --repo "$cwd" --yes
    pause ;;
  ssh-config)
    "$cs" ssh-config "$machine"; pause ;;
esac
