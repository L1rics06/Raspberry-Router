#!/usr/bin/env bash
set -euo pipefail
[[ "$EUID" -eq 0 ]] || { echo 'Usage: sudo bash scripts/enable_pi5_pwm.sh LOGIN_USER' >&2; exit 2; }
[[ "$(tr -d '\0' </proc/device-tree/model)" == 'Raspberry Pi 5'* ]] || { echo 'Raspberry Pi 5 required' >&2; exit 2; }
login_user="${1:-${SUDO_USER:-}}"
[[ "$login_user" =~ ^[a-z_][a-z0-9_-]*$ ]] && id "$login_user" >/dev/null || { echo 'Provide an existing non-root login user' >&2; exit 2; }
[[ "$login_user" != root ]] || { echo 'Use your normal login account' >&2; exit 2; }
command -v pinctrl >/dev/null || { echo 'Install raspi-utils first: sudo apt install raspi-utils' >&2; exit 2; }
bundle_path="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
config_path='/boot/firmware/config.txt'
[[ -f "$config_path" ]] || config_path='/boot/config.txt'
[[ -f "$config_path" ]] || { echo 'config.txt not found' >&2; exit 2; }
overlay_line='dtoverlay=pwm,pin=12,func=4'
if ! grep -Fxq "$overlay_line" "$config_path"; then
  if grep -Eq '^[[:space:]]*dtoverlay=pwm(-2chan)?(,|$)' "$config_path"; then
    echo 'Existing PWM overlay found; resolve its pin ownership before using this installer' >&2
    exit 2
  fi
  backup_path="$config_path.maker-servo.$(date +%Y%m%d_%H%M%S).bak"
  cp -p -- "$config_path" "$backup_path"
  printf '\n[all]\n# Maker wall switch: BCM12 physical32\n%s\n' "$overlay_line" >>"$config_path"
  echo "Backed up config: $backup_path"
fi
getent group gpio >/dev/null || groupadd --system gpio
usermod -a -G gpio "$login_user"
install -d -m 0755 /usr/local/libexec
install -m 0755 "$bundle_path/scripts/prepare_pi5_pwm.sh" /usr/local/libexec/maker-servo-prepare-pwm
cat >/etc/systemd/system/maker-servo-pwm-prepare.service <<'UNIT'
[Unit]
Description=Prepare Pi5 BCM12 servo PWM without enabling output
After=systemd-udev-settle.service
Before=piagent-task4.service

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/maker-servo-prepare-pwm
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable maker-servo-pwm-prepare.service
echo 'Installed. Disconnect servo power, reboot the Pi, log in again, then run servo doctor'
echo 'This script has not started servo pulses and has not rebooted the Pi'
