#!/usr/bin/env bash
# NEXUS Live : construit une image ISO hybride (BIOS + UEFI, Secure Boot) Debian 13
# avec bureau XFCE, Ollama local et un modèle préchargé.
#
#   sudo ./live/build-iso.sh                      # modèle qwen3:1.7b
#   sudo ./live/build-iso.sh --model qwen3:4b     # plus capable, 8 Go de RAM conseillés
#   ./live/build-iso.sh --prepare-only            # génère la configuration, sans root ni réseau
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
OUT="$ROOT/build"
WORK="$OUT/live"
CACHE="${NEXUS_CACHE:-$OUT/cache}"
MODEL='qwen3:1.7b'
INCLUDE_MODEL=1
PREPARE_ONLY=0
KEEP_GPU=0
OLLAMA_VERSION=''
OLLAMA_SHA256=''
MIRROR='http://deb.debian.org/debian'
ISO_NAME='nexus-live-amd64'
SQUASHFS_LIMIT=$((4 * 1024 * 1024 * 1024 - 1))   # limite d'un fichier ISO 9660 niveau 2

usage() {
    cat <<'EOF'
Utilisation : sudo ./live/build-iso.sh [options]

  --model NOM            modèle Ollama préchargé (défaut : qwen3:1.7b)
  --no-model             pas de modèle intégré (à télécharger après le démarrage)
  --ollama-version X.Y.Z version d'Ollama à télécharger (défaut : la dernière)
  --ollama-sha256 HASH   empreinte attendue de l'archive Ollama (recommandé)
  --keep-gpu             garder les bibliothèques CUDA/ROCm d'Ollama (~2,5 Go de plus)
  --mirror URL           miroir Debian (défaut : http://deb.debian.org/debian)
  --prepare-only         préparer la configuration seulement : ni root, ni réseau, ni ISO
  -h, --help             cette aide

Le cache (paquets Debian, Ollama, modèles) est gardé dans build/cache
ou dans $NEXUS_CACHE. Résultat : build/nexus-live-amd64.iso
EOF
}

die() { echo "Erreur : $*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }

while (($#)); do
    case "$1" in
        --model)          (($# >= 2)) || die '--model attend un nom'; MODEL=$2; shift 2 ;;
        --no-model)       INCLUDE_MODEL=0; shift ;;
        --ollama-version) (($# >= 2)) || die '--ollama-version attend une version'; OLLAMA_VERSION=$2; shift 2 ;;
        --ollama-sha256)  (($# >= 2)) || die '--ollama-sha256 attend une empreinte'; OLLAMA_SHA256=${2,,}; shift 2 ;;
        --keep-gpu)       KEEP_GPU=1; shift ;;
        --mirror)         (($# >= 2)) || die '--mirror attend une URL'; MIRROR=$2; shift 2 ;;
        --prepare-only)   PREPARE_ONLY=1; shift ;;
        -h|--help)        usage; exit 0 ;;
        *)                usage >&2; die "option inconnue : $1" ;;
    esac
done

# Validation de tout ce qui finit dans des fichiers ou des lignes de commande.
[[ $MODEL =~ ^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}$ ]] || die 'nom de modèle invalide'
[[ ${MODEL,,} != *:cloud && ${MODEL,,} != *-cloud ]] || die 'les modèles cloud sont refusés'
[[ -z $OLLAMA_VERSION || $OLLAMA_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$ ]] || die 'version Ollama invalide'
[[ -z $OLLAMA_SHA256 || $OLLAMA_SHA256 =~ ^[0-9a-f]{64}$ ]] || die 'empreinte SHA256 invalide'
[[ $MIRROR =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$ ]] || die 'URL de miroir invalide'

# ---------------------------------------------------------------------------
say 'Préparation de la configuration live-build'
if [[ -d $WORK ]]; then
    # On repart d'une configuration propre, en gardant le cache de paquets de live-build.
    if [[ -d $WORK/cache ]]; then  # cache de paquets gardé dans tous les cas
        mkdir -p "$CACHE"
        rm -rf -- "$CACHE/lb-cache"
        mv -- "$WORK/cache" "$CACHE/lb-cache"
    fi
    if ((!PREPARE_ONLY)) && command -v lb >/dev/null && ((EUID == 0)); then
        (cd "$WORK" && lb clean --purge >/dev/null 2>&1) || true
    fi
    rm -rf --one-file-system -- "$WORK"
fi
mkdir -p "$WORK" "$CACHE"
[[ -d $CACHE/lb-cache ]] && mv -- "$CACHE/lb-cache" "$WORK/cache"

CONFIG="$WORK/config"
INC="$CONFIG/includes.chroot_after_packages"
mkdir -p "$CONFIG/package-lists" "$CONFIG/hooks/live" "$INC"

cat > "$CONFIG/package-lists/nexus.list.chroot" <<'EOF'
# Système et matériel
linux-image-amd64
live-boot
live-config
live-config-systemd
user-setup
sudo
locales
console-setup
keyboard-configuration
intel-microcode
amd64-microcode
# Bureau (sans la tâche complète, qui tirerait LibreOffice et consorts)
xorg
lightdm
xfce4
xfce4-terminal
xfce4-power-manager
xfce4-pulseaudio-plugin
xfce4-whiskermenu-plugin
thunar
mousepad
ristretto
network-manager-gnome
pipewire-pulse
wireplumber
pavucontrol
firefox-esr
firefox-esr-l10n-fr
fonts-dejavu-core
fonts-noto-color-emoji
# NEXUS
python3
python3-tk
ca-certificates
curl
zstd
# Outils de consultation de NEXUS (lecture seule)
pciutils
usbutils
poppler-utils
tesseract-ocr
tesseract-ocr-fra
EOF

# --- application NEXUS --------------------------------------------------------
mkdir -p "$INC/opt/nexus" "$INC/usr/local/bin" "$INC/usr/share/applications" \
         "$INC/etc/xdg/autostart" "$INC/etc/nexus" "$INC/etc/systemd/system" \
         "$INC/var/lib/ollama/models"
cp -a -- "$ROOT/nexus" "$INC/opt/nexus/nexus"
find "$INC/opt/nexus" -name '__pycache__' -prune -exec rm -rf -- {} +
printf '%s\n' "$MODEL" > "$INC/etc/nexus/model"

cat > "$INC/usr/local/bin/nexus" <<'EOF'
#!/bin/sh
# Ligne de commande NEXUS : nexus status | chat | ask | watch | history | forget
PYTHONPATH=/opt/nexus exec /usr/bin/python3 -m nexus "$@"
EOF
cat > "$INC/usr/local/bin/nexus-desktop" <<'EOF'
#!/bin/sh
PYTHONPATH=/opt/nexus exec /usr/bin/python3 -m nexus desktop
EOF
chmod 0755 "$INC/usr/local/bin/nexus" "$INC/usr/local/bin/nexus-desktop"

cat > "$INC/usr/share/applications/nexus.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=NEXUS
Comment=Assistant IA local, sans Internet
Exec=/usr/local/bin/nexus-desktop
Icon=utilities-system-monitor
Terminal=false
Categories=Utility;
EOF
cp -- "$INC/usr/share/applications/nexus.desktop" "$INC/etc/xdg/autostart/nexus.desktop"

# --- service Ollama : local uniquement, utilisateur dédié, système en lecture seule ---
cat > "$INC/etc/systemd/system/ollama.service" <<'EOF'
[Unit]
Description=Ollama (local uniquement) pour NEXUS
After=local-fs.target

[Service]
Type=exec
User=ollama
Group=ollama
ExecStart=/usr/bin/ollama serve
Environment=HOME=/var/lib/ollama
Environment=OLLAMA_HOST=127.0.0.1:11434
Environment=OLLAMA_MODELS=/var/lib/ollama/models
Environment=OLLAMA_NO_CLOUD=1
Environment=OLLAMA_KEEP_ALIVE=30m
Environment=OLLAMA_MAX_LOADED_MODELS=1
Environment=OLLAMA_NUM_PARALLEL=1
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/ollama
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
EOF

cat > "$CONFIG/hooks/live/0500-nexus.hook.chroot" <<'EOF'
#!/bin/sh
set -eu
id ollama >/dev/null 2>&1 ||
    useradd --system --user-group --home-dir /var/lib/ollama --shell /usr/sbin/nologin ollama
mkdir -p /var/lib/ollama/models
chown -R ollama:ollama /var/lib/ollama
systemctl enable ollama.service
# Français par défaut, y compris si l'on oublie les paramètres de démarrage.
sed -i 's/^# *fr_FR.UTF-8 UTF-8/fr_FR.UTF-8 UTF-8/' /etc/locale.gen
locale-gen
update-locale LANG=fr_FR.UTF-8
# Pas d'installateur sur le bureau : la clé ne touche jamais au disque interne.
rm -f /usr/share/applications/*calamares*.desktop /usr/share/applications/debian-installer*.desktop
python3 -m compileall -q /opt/nexus/nexus
# Pas de verrouillage d'écran : en session Live, l'utilisateur ne connaît pas le mot de passe.
apt-get purge -y light-locker xfce4-screensaver xscreensaver >/dev/null 2>&1 || true
# L'utilisateur Live peut LIRE le journal système (outils « erreurs récentes » et
# « firmwares manquants » de NEXUS). On reprend la liste par défaut de live-config,
# limitée aux groupes qui existent vraiment dans l'image (sinon : erreurs au démarrage).
groups=''
for group in audio cdrom dip floppy video plugdev netdev powerdev scanner bluetooth lpadmin systemd-journal; do
    getent group "$group" >/dev/null && groups="$groups $group"
done
mkdir -p /etc/live/config.conf.d
printf 'LIVE_USER_DEFAULT_GROUPS="%s"\n' "${groups# }" > /etc/live/config.conf.d/nexus.conf
EOF
chmod 0755 "$CONFIG/hooks/live/0500-nexus.hook.chroot"

# Menu de démarrage : entrée Live choisie d'office après 5 secondes, sans le bip par défaut.
mkdir -p "$CONFIG/bootloaders/grub-pc"
cat > "$CONFIG/bootloaders/grub-pc/config.cfg" <<'EOF'
set default=0
set timeout=5

if [ x$feature_default_font_path = xy ] ; then
    font=unicode
else
    font=$prefix/unicode.pf2
fi

if loadfont $font ; then
    set gfxmode=800x600
    set gfxpayload=keep
    insmod efi_gop
    insmod efi_uga
    insmod video_bochs
    insmod video_cirrus
else
    set gfxmode=auto
    insmod all_video
fi

insmod gfxterm
insmod png

source /boot/grub/theme.cfg

terminal_output gfxterm
EOF

mkdir -p "$WORK/auto"
cat > "$WORK/auto/config" <<EOF
#!/bin/sh
set -e
lb config noauto \\
  --mode debian \\
  --distribution trixie \\
  --architectures amd64 \\
  --archive-areas 'main non-free-firmware' \\
  --mirror-bootstrap '$MIRROR' \\
  --mirror-chroot '$MIRROR' \\
  --mirror-binary '$MIRROR' \\
  --security true \\
  --updates true \\
  --firmware-chroot true \\
  --firmware-binary false \\
  --binary-image iso-hybrid \\
  --bootloaders 'grub-pc,grub-efi' \\
  --uefi-secure-boot enable \\
  --debian-installer none \\
  --chroot-squashfs-compression-type zstd \\
  --chroot-squashfs-compression-level 19 \\
  --memtest none \\
  --checksums sha256 \\
  --zsync false \\
  --image-name '$ISO_NAME' \\
  --iso-application 'NEXUS Live' \\
  --iso-publisher 'NEXUS' \\
  --iso-volume 'NEXUS_LIVE' \\
  --bootappend-live 'boot=live components quiet splash locales=fr_FR.UTF-8 keyboard-layouts=fr timezone=Europe/Paris username=nexus hostname=nexus persistence' \\
  "\$@"
EOF
chmod 0755 "$WORK/auto/config"

if ((PREPARE_ONLY)); then
    echo "Configuration prête dans $CONFIG (aucune ISO produite)."
    exit 0
fi

# ---------------------------------------------------------------------------
((EUID == 0)) || die 'la construction demande les droits root (sudo)'
[[ $(uname -m) == x86_64 ]] || die 'hôte x86_64 requis'
for tool in lb debootstrap curl tar zstd sha256sum; do
    command -v "$tool" >/dev/null || die "outil manquant : $tool (voir live/install-build-deps.sh)"
done
FREE_GIB=$(( $(df --output=avail -k "$OUT" | tail -1) / 1024 / 1024 ))
((FREE_GIB >= 15)) || die "seulement $FREE_GIB Go libres dans $OUT ; 15 Go au moins sont nécessaires"

# --- Ollama -----------------------------------------------------------------
say 'Ollama'
if [[ -n $OLLAMA_VERSION ]]; then
    URL="https://github.com/ollama/ollama/releases/download/v$OLLAMA_VERSION/ollama-linux-amd64.tar.zst"
    ARCHIVE="$CACHE/ollama-$OLLAMA_VERSION-linux-amd64.tar.zst"
else
    URL='https://ollama.com/download/ollama-linux-amd64.tar.zst'
    ARCHIVE="$CACHE/ollama-linux-amd64.tar.zst"
fi
if [[ ! -s $ARCHIVE ]]; then
    curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --progress-bar \
         -o "$ARCHIVE.part" "$URL"
    mv -- "$ARCHIVE.part" "$ARCHIVE"
fi
ACTUAL_SHA=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
if [[ -n $OLLAMA_SHA256 ]]; then
    [[ $ACTUAL_SHA == "$OLLAMA_SHA256" ]] || die "empreinte Ollama inattendue : $ACTUAL_SHA"
else
    echo "Attention : archive non vérifiée par empreinte. SHA256 = $ACTUAL_SHA" >&2
fi
mkdir -p "$INC/usr"
tar --zstd -xf "$ARCHIVE" -C "$INC/usr" --no-same-owner
[[ -x $INC/usr/bin/ollama ]] || die 'archive Ollama inattendue : bin/ollama absent'
if ((!KEEP_GPU)); then
    # Sans pilote NVIDIA/AMD dans l'image, ces bibliothèques (plusieurs Go) ne servent à rien.
    find "$INC/usr/lib/ollama" -mindepth 1 -maxdepth 1 -type d \
         \( -name 'cuda*' -o -name 'rocm*' \) -exec rm -rf -- {} +
fi
OLLAMA_REPORTED=$("$INC/usr/bin/ollama" --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | tail -1 || true)
echo "Ollama ${OLLAMA_REPORTED:-?} ($(du -sh "$INC/usr/lib/ollama" | cut -f1) de bibliothèques)"

# --- modèle -------------------------------------------------------------------
MODEL_BYTES=0
if ((INCLUDE_MODEL)); then
    say "Modèle $MODEL"
    STORE="$CACHE/models"
    mkdir -p "$STORE"
    PORT=$(( 20000 + RANDOM % 20000 ))
    OLLAMA_HOST="127.0.0.1:$PORT" OLLAMA_MODELS="$STORE" OLLAMA_NO_CLOUD=1 HOME="$CACHE" \
        "$INC/usr/bin/ollama" serve >"$WORK/ollama-pull.log" 2>&1 &
    SERVER=$!
    trap 'kill "$SERVER" 2>/dev/null || true' EXIT
    for _ in $(seq 60); do
        curl -fs --noproxy '*' "http://127.0.0.1:$PORT/api/version" >/dev/null && break
        kill -0 "$SERVER" 2>/dev/null || die "le serveur Ollama temporaire s'est arrêté (voir $WORK/ollama-pull.log)"
        sleep 1
    done
    OLLAMA_HOST="127.0.0.1:$PORT" "$INC/usr/bin/ollama" pull "$MODEL"
    kill "$SERVER"; wait "$SERVER" 2>/dev/null || true
    trap - EXIT
    # On ne copie que ce modèle (manifeste + blobs), même si le cache en contient d'autres.
    NAME=${MODEL%%:*}; TAG=${MODEL#*:}; [[ $MODEL == *:* ]] || TAG=latest
    case $NAME in */*) MANIFEST_DIR="registry.ollama.ai/$NAME" ;; *) MANIFEST_DIR="registry.ollama.ai/library/$NAME" ;; esac
    MANIFEST="$STORE/manifests/$MANIFEST_DIR/$TAG"
    [[ -f $MANIFEST ]] || die "manifeste introuvable : $MANIFEST"
    mkdir -p "$INC/var/lib/ollama/models/manifests/$(dirname "$MANIFEST_DIR/$TAG")" "$INC/var/lib/ollama/models/blobs"
    cp -- "$MANIFEST" "$INC/var/lib/ollama/models/manifests/$MANIFEST_DIR/$TAG"
    grep -Eo 'sha256:[0-9a-f]{64}' "$MANIFEST" | sort -u | while read -r digest; do
        cp -- "$STORE/blobs/${digest/:/-}" "$INC/var/lib/ollama/models/blobs/"
    done
    MODEL_BYTES=$(du -sb "$INC/var/lib/ollama/models" | cut -f1)
    echo "Modèle intégré : $((MODEL_BYTES / 1024 / 1024)) Mo"
else
    : > "$INC/etc/nexus/model"
fi

GIT_COMMIT=$(git -C "$ROOT" describe --always --dirty 2>/dev/null || echo '?')
cat > "$INC/etc/nexus/build-info" <<EOF
NEXUS_COMMIT=$GIT_COMMIT
BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
OLLAMA_VERSION=${OLLAMA_REPORTED:-?}
OLLAMA_SHA256=$ACTUAL_SHA
MODEL=$( ((INCLUDE_MODEL)) && echo "$MODEL" || echo none)
MODEL_BYTES=$MODEL_BYTES
EOF

# --- construction --------------------------------------------------------------
say "Construction de l'image (de 20 à 60 minutes)"
cd "$WORK"
lb config
set +e
lb build 2>&1 | tee "$WORK/build.log"
STATUS=${PIPESTATUS[0]}
set -e
((STATUS == 0)) || die "lb build a échoué (code $STATUS) ; voir $WORK/build.log"

ISO="$WORK/$ISO_NAME-amd64.hybrid.iso"
[[ -s $ISO ]] || ISO=$(find "$WORK" -maxdepth 1 -name '*.iso' -size +100M | head -1)
[[ -n $ISO && -s $ISO ]] || die "aucune ISO produite ; voir $WORK/build.log"
SQUASH="$WORK/binary/live/filesystem.squashfs"
if [[ -f $SQUASH ]] && (( $(stat -c %s "$SQUASH") > SQUASHFS_LIMIT )); then
    die "filesystem.squashfs dépasse 4 Gio : choisir un modèle plus petit"
fi

mv -- "$ISO" "$OUT/$ISO_NAME.iso"
(cd "$OUT" && sha256sum "$ISO_NAME.iso" > "$ISO_NAME.iso.sha256")
cp -- "$INC/etc/nexus/build-info" "$OUT/$ISO_NAME.build-info"
say "Terminé : $OUT/$ISO_NAME.iso ($(du -h "$OUT/$ISO_NAME.iso" | cut -f1))"
echo "Empreinte : $(cut -d' ' -f1 "$OUT/$ISO_NAME.iso.sha256")"
echo 'Tester avant de graver :  ./live/test-iso.sh build/nexus-live-amd64.iso [--uefi]'
