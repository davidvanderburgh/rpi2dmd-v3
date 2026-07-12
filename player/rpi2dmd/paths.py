"""Path resolution honoring dev-machine overrides (see docs/contracts.md)."""

import os


def media_root():
    return os.environ.get("RPI2DMD_MEDIA", "/media/usb")


def run_dir():
    d = os.environ.get("RPI2DMD_RUN", "/run/rpi2dmd")
    if not os.path.isdir(d):
        try:
            os.makedirs(d)
        except OSError:
            pass
    return d


def dmd_dir():
    return os.path.join(media_root(), "dmd")


def gif_dir():
    return os.path.join(media_root(), "gif")


def fonts_dir():
    return os.path.join(media_root(), "fonts")


def config_path():
    return os.path.join(media_root(), "config", "rpi2dmd.json")


def v2_config_path():
    return os.path.join(media_root(), "config", "config.txt")


def status_path():
    return os.path.join(run_dir(), "status.json")


def control_socket_path():
    return os.path.join(run_dir(), "control.sock")


def control_tcp():
    """(host, port) TCP fallback when AF_UNIX is unavailable/forced."""
    spec = os.environ.get("RPI2DMD_CTL_TCP", "127.0.0.1:9077")
    host, _, port = spec.partition(":")
    return host or "127.0.0.1", int(port or 9077)


def use_tcp_control():
    import socket
    return "RPI2DMD_CTL_TCP" in os.environ or not hasattr(socket, "AF_UNIX")
