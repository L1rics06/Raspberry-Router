#!/usr/bin/env bash
set -euo pipefail
[[ "$EUID" -eq 0 ]] || { echo 'Run this preparation helper as root' >&2; exit 2; }
[[ "$(tr -d '\0' </proc/device-tree/model)" == 'Raspberry Pi 5'* ]] || { echo 'Raspberry Pi 5 required' >&2; exit 2; }
lock_path='/run/lock/maker-servo-gpio12.lock'
stop_path='/run/lock/maker-servo-gpio12.stop'
for control_file in "$lock_path" "$stop_path"; do
  [[ ! -L "$control_file" ]] || { echo 'Refusing symlink control file' >&2; exit 2; }
done
touch "$lock_path"
if [[ ! -e "$stop_path" ]]; then printf '0' >"$stop_path"; fi
chown root:gpio "$lock_path" "$stop_path"
chmod 0660 "$lock_path" "$stop_path"
exec 9<>"$lock_path"
flock -n 9 || { echo 'Servo controller is active; stop it before preparing PWM' >&2; exit 2; }
chip_path=''
for candidate_path in /sys/class/pwm/pwmchip*; do
  [[ -d "$candidate_path" ]] || continue
  device_name="$(basename -- "$(readlink -f -- "$candidate_path/device")")"
  if [[ "$device_name" == *98000.pwm ]] && grep -aq 'raspberrypi,rp1-pwm' "$candidate_path/device/of_node/compatible"; then
    [[ -z "$chip_path" ]] || { echo 'Multiple RP1 PWM0 controllers found' >&2; exit 2; }
    chip_path="$candidate_path"
  fi
done
[[ -n "$chip_path" ]] || { echo 'RP1 PWM0 missing; enable overlay and reboot first' >&2; exit 2; }
if [[ ! -d "$chip_path/pwm0" ]]; then
  printf '0' >"$chip_path/export"
  for _attempt in {1..50}; do
    [[ -f "$chip_path/pwm0/enable" ]] && break
    sleep 0.02
  done
fi
channel_path="$chip_path/pwm0"
[[ "$(cat "$channel_path/enable")" == 0 ]] || { echo 'PWM0 is enabled; refusing takeover' >&2; exit 2; }
printf '0' >"$channel_path/duty_cycle"
printf '20000000' >"$channel_path/period"
if [[ -f "$channel_path/polarity" ]]; then printf 'normal' >"$channel_path/polarity"; fi
for attribute in enable period duty_cycle; do
  chown root:gpio "$channel_path/$attribute"
  chmod 0660 "$channel_path/$attribute"
done
echo "Prepared $chip_path/pwm0: BCM12 / physical pin32 / 50Hz / disabled"
