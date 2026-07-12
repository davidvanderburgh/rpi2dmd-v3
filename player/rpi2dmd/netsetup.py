"""Network bootstrap at player startup (v2 go.sh parity).

Applies network.* config (hostname, Wi-Fi country/SSID/PSK, timezone) to
the running system and reboots when something changed, so a headless
first-boot config edit takes effect. Linux only; every filesystem action
is guarded so this never crashes on a dev machine.

Python 3.7 compatible; stdlib only.
"""

import os
import re
import subprocess
import sys

WPA_CONF = "/etc/wpa_supplicant/wpa_supplicant.conf"
HOSTNAME_FILE = "/etc/hostname"
HOSTS_FILE = "/etc/hosts"
TIMEZONE_FILE = "/etc/timezone"

# RFC1123 single label: letters/digits/hyphens, no leading/trailing hyphen
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _wpa_safe(value):
    """True if a value can be embedded in a quoted wpa_supplicant string:
    no control characters (newline injection) and no double quotes."""
    return not any(ch in value for ch in "\"\r\n\x00") and value.isprintable()

WPA_TEMPLATE = """ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=%s

network={
\tssid="%s"
\t%s
\tscan_ssid=1
}
"""


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _write(path, content):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except OSError:
        return False


def _apply_hostname(net):
    hostname = str(net.get("hostname", "") or "").strip()
    if not hostname or not os.path.exists(HOSTNAME_FILE):
        return False
    if not _HOSTNAME_RE.match(hostname):
        sys.stderr.write("network: invalid hostname %r ignored\n" % hostname)
        return False
    current = (_read(HOSTNAME_FILE) or "").strip()
    if current == hostname:
        return False
    if not _write(HOSTNAME_FILE, hostname + "\n"):
        return False
    if current and os.path.exists(HOSTS_FILE):
        hosts = _read(HOSTS_FILE)
        if hosts is not None and current in hosts:
            _write(HOSTS_FILE, hosts.replace(current, hostname))
    return True


def _apply_wifi(net):
    ssid = str(net.get("wifi_ssid", "") or "").strip()
    if not ssid or not os.path.exists(WPA_CONF):
        return False
    psk = str(net.get("wifi_psk", "") or "")
    country = str(net.get("wifi_country", "US") or "US").upper()
    if not _wpa_safe(ssid) or not _wpa_safe(psk):
        sys.stderr.write("network: SSID/PSK contains characters that cannot "
                         "be written safely (quote/control); ignored\n")
        return False
    if not re.match(r"^[A-Z]{2}$", country):
        country = "US"
    if psk:
        key_line = 'psk="%s"' % psk
    else:
        key_line = "key_mgmt=NONE"
    desired = WPA_TEMPLATE % (country, ssid, key_line)
    current = _read(WPA_CONF)
    if current == desired:
        return False
    return _write(WPA_CONF, desired)


def _apply_timezone(net):
    tz = str(net.get("timezone", "") or "").strip()
    if not tz or not os.path.exists(TIMEZONE_FILE):
        return False
    current = (_read(TIMEZONE_FILE) or "").strip()
    if current == tz:
        return False
    for exe in ("/usr/bin/timedatectl", "/bin/timedatectl"):
        if os.path.exists(exe):
            try:
                subprocess.call([exe, "set-timezone", tz])
            except OSError:
                pass
            return False  # timedatectl applies live, no reboot needed
    return False


def apply(cfg):
    """Apply network config; reboot (and return True) if a reboot-worthy
    change was made. No-op on non-Linux platforms."""
    if not sys.platform.startswith("linux"):
        return False
    net = cfg.get("network", {}) or {}
    changed = False
    try:
        if _apply_hostname(net):
            changed = True
        if _apply_wifi(net):
            changed = True
        _apply_timezone(net)
    except Exception as e:
        sys.stderr.write("network bootstrap failed: %s\n" % e)
        return False
    if changed:
        for exe in ("/sbin/reboot", "/usr/sbin/reboot", "/bin/reboot"):
            if os.path.exists(exe):
                sys.stderr.write("network config changed; rebooting\n")
                try:
                    subprocess.Popen([exe])
                except OSError:
                    pass
                break
    return changed
