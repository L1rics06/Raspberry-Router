#!/usr/bin/env bash
set -euo pipefail
# systemd退出补充清理，不修改停止锁定，不接管其他活动控制者
lock_path='/run/lock/maker-servo-gpio12.lock'
[[ -f "$lock_path" ]] || exit 0
exec 9<>"$lock_path"
flock -n 9 || exit 0
for chip_path in /sys/class/pwm/pwmchip*; do
  [[ -d "$chip_path" ]] || continue
  device_name="$(basename -- "$(readlink -f -- "$chip_path/device")")"
  if [[ "$device_name" == *98000.pwm && -f "$chip_path/pwm0/enable" ]]; then
    printf '0' >"$chip_path/pwm0/duty_cycle"
    printf '0' >"$chip_path/pwm0/enable"
  fi
done
