#!/usr/bin/env bash
# Outils nécessaires pour construire et tester l'ISO, sur un hôte Debian 13 (ou Ubuntu 24.04+).
set -Eeuo pipefail
((EUID == 0)) || { echo 'Lancer avec sudo.' >&2; exit 1; }
apt-get update
apt-get install -y live-build debootstrap debian-archive-keyring curl ca-certificates zstd \
    xorriso squashfs-tools mtools dosfstools
# Facultatif, pour ./live/test-iso.sh
apt-get install -y qemu-system-x86 ovmf || echo 'QEMU/OVMF non installés : test en VM indisponible.' >&2
