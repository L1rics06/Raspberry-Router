#!/usr/bin/env bash
set -euo pipefail
bundle_path="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
service_name='piagent-task4.service'
installed_unit="/etc/systemd/system/$service_name"
action="${1:-help}"

# check_conflict 表示检查已有Agent，避免两个服务重复注册或争用GPIO
# 无参数，仅判断常见服务名，其余服务需按部署文档现场检查
check_conflict() {
  for other in piagent-agent.service piagent.service; do
    if systemctl is-active --quiet "$other"; then
      echo "Existing Agent is active: $other; coordinate migration before starting task4" >&2
      exit 2
    fi
  done
}

case "$action" in
  install)
    check_conflict
    source_unit="${2:-$bundle_path/.runtime/generated/piagent-task4.service}"
    source_unit="$(realpath -e -- "$source_unit")"
    if [[ "$(basename -- "$source_unit")" != "$service_name" ]]; then
      echo 'Only a generated piagent-task4.service may be installed' >&2
      exit 2
    fi
    if [[ -e "$installed_unit" ]]; then
      echo 'Existing task4 unit found; stop/uninstall it explicitly before replacing' >&2
      exit 2
    fi
    systemd-analyze verify "$source_unit"
    sudo install -m 0644 -- "$source_unit" "$installed_unit"
    sudo systemctl daemon-reload
    echo 'Installed only; use: bash scripts/service.sh start'
    ;;
  start)
    check_conflict
    sudo systemctl enable --now "$service_name"
    ;;
  status) systemctl status --no-pager "$service_name" ;;
  logs) sudo journalctl -u "$service_name" -n 100 --no-pager ;;
  stop) sudo systemctl disable --now "$service_name" ;;
  uninstall)
    sudo systemctl disable --now "$service_name" || true
    sudo rm -f -- "$installed_unit"
    sudo systemctl daemon-reload
    echo 'Removed only task4 service; private config and original services retained'
    ;;
  *) echo 'Usage: bash scripts/service.sh install [generated-unit] | start | status | logs | stop | uninstall' ;;
esac
