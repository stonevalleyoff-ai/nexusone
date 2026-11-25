#!/usr/bin/env bash
# Démarre l'ISO dans une machine virtuelle QEMU, sans toucher aux disques de l'hôte.
#   ./live/test-iso.sh build/nexus-live-amd64.iso            # BIOS
#   ./live/test-iso.sh build/nexus-live-amd64.iso --uefi     # UEFI (OVMF)
#   ./live/test-iso.sh build/nexus-live-amd64.iso --secure-boot   # UEFI + Secure Boot (clés Microsoft)
#   options : --ram MIO (défaut 6144), --cpus N (défaut 4)
# shellcheck disable=SC2054  # les virgules font partie des options QEMU
set -Eeuo pipefail
(($# >= 1)) || { sed -n '2,6p' "$0"; exit 2; }
ISO=$(realpath -- "$1"); shift
[[ -s $ISO ]] || { echo "ISO introuvable : $ISO" >&2; exit 2; }
UEFI=0 SECURE=0 RAM=6144 CPUS=4
EXTRA=()
while (($#)); do
    case "$1" in
        --uefi) UEFI=1; shift ;;
        --secure-boot) UEFI=1; SECURE=1; shift ;;
        --ram)  RAM=$2; shift 2 ;;
        --cpus) CPUS=$2; shift 2 ;;
        --) shift; EXTRA=("$@"); break ;;   # options QEMU supplémentaires
        *) echo "option inconnue : $1" >&2; exit 2 ;;
    esac
done
[[ $RAM =~ ^[0-9]+$ && $CPUS =~ ^[0-9]+$ ]] || { echo 'valeurs numériques attendues' >&2; exit 2; }
command -v qemu-system-x86_64 >/dev/null || { echo 'Installer qemu-system-x86.' >&2; exit 2; }
ARGS=(-machine q35 -m "$RAM" -smp "$CPUS" -cdrom "$ISO" -boot d
      -nic user,model=virtio-net-pci -device virtio-vga -usb -device usb-tablet)
[[ -e /dev/kvm && -w /dev/kvm ]] && ARGS+=(-enable-kvm -cpu host) || echo 'Sans KVM : démarrage lent (émulation).' >&2
if ((UEFI)); then
    if ((SECURE)); then
        CODE=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd
        TEMPLATE=/usr/share/OVMF/OVMF_VARS_4M.ms.fd   # clés Microsoft préinstallées, Secure Boot actif
        ARGS[1]=q35,smm=on
        ARGS+=(-global driver=cfi.pflash01,property=secure,value=on)
    else
        CODE=/usr/share/OVMF/OVMF_CODE_4M.fd
        TEMPLATE=/usr/share/OVMF/OVMF_VARS_4M.fd
    fi
    [[ -f $CODE && -f $TEMPLATE ]] || { echo 'Installer le paquet ovmf pour le test UEFI.' >&2; exit 2; }
    VARS=$(mktemp --suffix=.fd)
    trap 'rm -f "$VARS"' EXIT
    cp -- "$TEMPLATE" "$VARS"
    ARGS+=(-drive "if=pflash,format=raw,unit=0,readonly=on,file=$CODE"
           -drive "if=pflash,format=raw,unit=1,file=$VARS")
fi
ARGS+=("${EXTRA[@]}")
qemu-system-x86_64 "${ARGS[@]}"
