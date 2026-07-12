#!/bin/bash
# Runs INSIDE the armhf Buster chroot (via qemu-arm-static, invoked by
# build.sh). Installs the v3 runtime stack from the legacy Buster archives,
# builds the rpi-rgb-led-matrix Python bindings from the pinned tarball at
# /tmp/rgb-matrix.tar.gz, verifies imports, then purges the build toolchain
# and re-verifies.
#
# Usage (inside chroot): bash /tmp/chroot-setup.sh <rgb-matrix-sha>

set -e
RGB_SHA="${1:?usage: chroot-setup.sh <rgb-matrix-sha>}"

export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C LANG=C
export PYTHONDONTWRITEBYTECODE=1

APT="apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=3"

echo "== [chroot] pointing apt at the legacy Buster archives"
cat > /etc/apt/sources.list <<'EOF'
deb http://legacy.raspbian.org/raspbian buster main contrib non-free rpi
EOF
cat > /etc/apt/sources.list.d/raspi.list <<'EOF'
deb http://archive.raspberrypi.org/debian buster main
EOF

echo "== [chroot] apt-get update"
# --allow-releaseinfo-change: the archived buster repos changed their
# Release metadata (e.g. Suite: testing -> '') after being frozen
$APT update --allow-releaseinfo-change

echo "== [chroot] installing runtime packages"
$APT install -y --no-install-recommends \
    python3 python3-flask python3-pil fatresize parted

echo "== [chroot] installing build toolchain (purged again below)"
$APT install -y --no-install-recommends \
    build-essential python3-dev python3-setuptools cython3

echo "== [chroot] building rpi-rgb-led-matrix ($RGB_SHA) python bindings"
tar -xzf /tmp/rgb-matrix.tar.gz -C /tmp
RGB_SRC=$(echo /tmp/rpi-rgb-led-matrix-*)
[ -d "$RGB_SRC" ] || { echo "rgb-matrix source dir missing"; exit 1; }
cd "$RGB_SRC/bindings/python"
make build-python PYTHON=/usr/bin/python3 -j4
# plain distutils-style install (no egg, no pkg_resources dependency)
python3 setup.py install --single-version-externally-managed \
    --record /tmp/rgbmatrix-installed-files.txt \
    || python3 setup.py install
cd /

echo "== [chroot] import check A (with build toolchain present)"
python3 -c 'import rgbmatrix, flask, PIL; print("CHROOT-CHECK-A OK flask=%s pil=%s" % (flask.__version__, PIL.__version__))'
python3 -c 'import sys; sys.path.insert(0, "/opt/rpi2dmd-v3/player"); import rpi2dmd.main; print("CHROOT-CHECK-B OK rpi2dmd.main imports")'

echo "== [chroot] recording package versions"
{
    echo "rgb-matrix-sha: $RGB_SHA"
    dpkg-query -W -f='${Package} ${Version}\n' \
        python3 python3-flask python3-pil python3-jinja2 python3-werkzeug \
        fatresize parted libparted2 \
        build-essential python3-dev python3-setuptools cython3 2>/dev/null
} > /tmp/rpi2dmd-versions.txt

echo "== [chroot] purging build toolchain"
# autoremove alone keeps gcc/make behind via Recommends chains, and the
# v2 base image ships without any toolchain — purge explicitly to match.
$APT purge -y build-essential python3-dev python3-setuptools cython3 \
    gcc g++ make cpp gcc-8 g++-8 cpp-8 dpkg-dev patch \
    libc6-dev libc-dev-bin linux-libc-dev libgcc-8-dev libstdc++-8-dev \
    python3.7-dev libpython3-dev libpython3.7-dev
$APT autoremove -y --purge
$APT clean
rm -rf /var/lib/apt/lists/*
rm -rf "$RGB_SRC" /tmp/rgb-matrix.tar.gz /tmp/rgbmatrix-installed-files.txt

echo "== [chroot] import re-check (after purge)"
python3 -c 'import rgbmatrix, flask, PIL; print("CHROOT-CHECK-C OK (post-purge)")'
python3 -c 'import sys; sys.path.insert(0, "/opt/rpi2dmd-v3/player"); import rpi2dmd.main; print("CHROOT-CHECK-D OK (post-purge)")'

echo "== [chroot] rootfs usage:"
df -h /
echo "== [chroot] chroot-setup.sh completed OK"
