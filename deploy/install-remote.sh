#!/usr/bin/env bash
set +x
set -euo pipefail

if (( EUID != 0 )); then
    printf 'Run this installer with sudo bash deploy/install-remote.sh\n' >&2
    exit 1
fi
trap 'printf "Installation failed; check the error above and journalctl -u network-diagnostics-remote.service.\n" >&2' ERR
if ! command -v systemctl >/dev/null; then
    printf 'systemctl is required; use Ubuntu Server with systemd.\n' >&2
    exit 1
fi
/usr/bin/python3 -c 'import sys; sys.exit("Python 3.10 or newer is required") if sys.version_info < (3, 10) else None'
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

if [[ ! -s /etc/network-diagnostics/remote.env ]]; then
    IFS= read -r -s -p 'Shared key (at least 32 bytes; use the same key on your client): ' diagnostic_key
    printf '\n' >&2
    LC_ALL=C
    if (( ${#diagnostic_key} < 32 )); then
        printf 'Key is too short; nothing was installed.\n' >&2
        exit 1
    fi
    diagnostic_key=${diagnostic_key//\\/\\\\}
    diagnostic_key=${diagnostic_key//\"/\\\"}
    install -d -o root -g root -m 0700 /etc/network-diagnostics
    umask 077
    printf 'NETDIAG_KEY="%s"\n' "$diagnostic_key" > /etc/network-diagnostics/remote.env
    unset diagnostic_key
fi
chown root:root /etc/network-diagnostics/remote.env
chmod 0600 /etc/network-diagnostics/remote.env
install -d -o root -g root -m 0755 /opt/network-diagnostics /opt/network-diagnostics/netdiag
install -o root -g root -m 0644 "$source_dir"/netdiag/{__init__,protocol,remote}.py /opt/network-diagnostics/netdiag/
install -o root -g root -m 0644 "$source_dir/deploy/network-diagnostics-remote.service" /etc/systemd/system/network-diagnostics-remote.service
systemctl daemon-reload
systemctl enable network-diagnostics-remote.service
systemctl restart network-diagnostics-remote.service
systemctl is-active --quiet network-diagnostics-remote.service
printf 'Companion running on TCP/UDP port 48123; enabled at boot. You can disconnect SSH.\n'
