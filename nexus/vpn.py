"""Le tunnel privé de la maison (WireGuard), sans aucun service tiers.

Le téléphone rejoint le PC par un tunnel chiffré qui ne laisse passer que lui : rien de
NEXUS n'est exposé sur Internet, à la maison comme dehors. NEXUS prépare tout ce qui peut
l'être sans droits d'administrateur :

- les clés du PC et de chaque appareil, créées par l'outil officiel `wg` ;
- le fichier de réglage du PC, et les deux commandes `sudo` à lancer une fois ;
- pour chaque téléphone, un réglage affiché une seule fois (code QR à lire avec l'appli
  WireGuard), que NEXUS ne garde pas : il ne conserve que la clé publique de l'appareil.

Choix de sécurité : un téléphone ne peut joindre que le PC (pas le reste du réseau de la
maison, pas Internet par la maison), chaque appareil a sa propre clé plus une clé partagée
supplémentaire, et un appareil se retire d'une commande.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable

from .ollama import OllamaError

INTERFACE = 'nexus'
NETWORK = ipaddress.ip_network('10.73.0.0/24')
SERVER_ADDRESS = str(NETWORK.network_address + 1)        # 10.73.0.1 : le PC, vu depuis le tunnel
DEFAULT_PORT = 51820
TOOLS_PATH = '/usr/bin:/bin:/usr/local/bin'
KEY_RE = re.compile(r'[A-Za-z0-9+/]{43}=')
HOST_RE = re.compile(r'(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}')

Run = Callable[[list[str], bytes], tuple[int, bytes]]


def _exec(argv: list[str], stdin: bytes = b'') -> tuple[int, bytes]:
    program = shutil.which(argv[0], path=TOOLS_PATH)
    if program is None:
        raise OllamaError(f'Il manque « {argv[0]} » : sudo apt install ' + ('wireguard-tools' if argv[0] == 'wg' else argv[0]))
    try:
        done = subprocess.run([program, *argv[1:]], input=stdin, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OllamaError(f'{argv[0]} n\'a pas pu être lancé ({type(exc).__name__})') from exc
    return done.returncode, done.stdout


def tidy_name(name: Any) -> str:
    text = ' '.join(str(name or '').split())
    return ''.join(ch for ch in text if ch.isprintable() and ch not in '[]#=\\')[:40].strip()


def check_endpoint(endpoint: str) -> str:
    """L'adresse publique de la maison : une adresse IP, ou un nom de domaine que tu possèdes."""
    endpoint = endpoint.strip().lower().strip('[]')
    try:
        return str(ipaddress.ip_address(endpoint))
    except ValueError:
        if HOST_RE.fullmatch(endpoint) and len(endpoint) <= 253:
            return endpoint
    raise ValueError('adresse publique refusée : donner une adresse IP ou un nom de domaine')


class Vpn:
    def __init__(self, base: Path, run: Run | None = None):
        self.base, self._run = Path(base), run or _exec
        self.state_path = self.base / 'etat.json'
        self.config_path = self.base / f'{INTERFACE}.conf'

    # -- outils -------------------------------------------------------------------------
    def _key(self, *argv: str, stdin: bytes = b'') -> str:
        code, out = self._run(['wg', *argv], stdin)
        key = out.decode('ascii', 'replace').strip()
        if code != 0 or not KEY_RE.fullmatch(key):
            raise OllamaError('L\'outil wg n\'a pas fourni de clé valable')
        return key

    def _write_private(self, path: Path, text: str) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        try:
            self.base.chmod(0o700)
        except OSError:
            pass
        temporary = path.with_suffix('.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)      # les clés restent privées
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(text)
        os.replace(temporary, path)

    # -- état ---------------------------------------------------------------------------
    def state(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if not (isinstance(data, dict) and KEY_RE.fullmatch(str(data.get('cle_privee', '')))
                and KEY_RE.fullmatch(str(data.get('cle_publique', ''))) and isinstance(data.get('appareils'), list)):
            return None
        return data

    def configured(self) -> bool:
        return self.state() is not None

    def devices(self) -> list[dict[str, Any]]:
        state = self.state()
        return [{'nom': d['nom'], 'adresse': d['adresse']} for d in state['appareils']] if state else []

    def _save(self, state: dict[str, Any]) -> None:
        self._write_private(self.state_path, json.dumps(state, ensure_ascii=False, indent=1))
        self._write_private(self.config_path, self.server_config(state))

    # -- réglage du PC ------------------------------------------------------------------
    def setup(self, endpoint: str, port: int = DEFAULT_PORT) -> dict[str, Any]:
        """Crée (une fois) les clés du PC ; relancé, ne change que l'adresse publique et le port."""
        endpoint = check_endpoint(endpoint)
        if not 1024 <= port <= 65535:
            raise ValueError('port hors limites (1024 à 65535)')
        state = self.state()
        if state is None:
            private = self._key('genkey')
            state = {'cle_privee': private, 'cle_publique': self._key('pubkey', stdin=private.encode() + b'\n'),
                     'appareils': []}
        state.update({'adresse_publique': endpoint, 'port': port})
        self._save(state)
        return state

    def server_config(self, state: dict[str, Any] | None = None) -> str:
        state = state or self.state()
        if state is None:
            raise OllamaError('Le tunnel n\'est pas encore préparé : nexus vpn installer ADRESSE_PUBLIQUE')
        lines = ['# Tunnel privé de NEXUS. Fichier écrit par `nexus vpn` : ne pas le modifier à la main.', '[Interface]',
                 f'Address = {SERVER_ADDRESS}/{NETWORK.prefixlen}', f"ListenPort = {state['port']}",
                 f"PrivateKey = {state['cle_privee']}"]
        for device in state['appareils']:
            lines += ['', f"# {device['nom']}", '[Peer]', f"PublicKey = {device['cle_publique']}",
                      f"PresharedKey = {device['cle_partagee']}", f"AllowedIPs = {device['adresse']}/32"]
        return '\n'.join(lines) + '\n'

    def install_commands(self) -> list[str]:
        """Les commandes d'administrateur à lancer une fois sur le PC (NEXUS ne les lance pas lui-même)."""
        return [f'sudo install -m 600 {self.config_path} /etc/wireguard/{INTERFACE}.conf',
                f'sudo systemctl enable --now wg-quick@{INTERFACE}']

    def reload_commands(self) -> list[str]:
        return [f'sudo install -m 600 {self.config_path} /etc/wireguard/{INTERFACE}.conf',
                f"sudo bash -c 'wg syncconf {INTERFACE} <(wg-quick strip {INTERFACE})'"]

    # -- appareils ----------------------------------------------------------------------
    def add_device(self, name: str) -> str:
        """Ajoute un appareil et renvoie son réglage, à lui transmettre tout de suite : NEXUS ne le garde pas."""
        state = self.state()
        if state is None:
            raise OllamaError('Le tunnel n\'est pas encore préparé : nexus vpn installer ADRESSE_PUBLIQUE')
        name = tidy_name(name)
        if not name:
            raise ValueError('nom d\'appareil vide')
        if any(d['nom'] == name for d in state['appareils']):
            raise ValueError(f'un appareil s\'appelle déjà « {name} » (nexus vpn retirer "{name}" d\'abord)')
        used = {d['adresse'] for d in state['appareils']}
        address = next((str(host) for host in list(NETWORK.hosts())[1:] if str(host) not in used), None)
        if address is None:
            raise ValueError('plus d\'adresse libre dans le tunnel')
        private = self._key('genkey')
        public = self._key('pubkey', stdin=private.encode() + b'\n')
        shared = self._key('genpsk')
        state['appareils'].append({'nom': name, 'adresse': address, 'cle_publique': public, 'cle_partagee': shared})
        self._save(state)
        endpoint = state['adresse_publique']
        if ':' in endpoint:
            endpoint = f'[{endpoint}]'            # adresse IPv6
        return '\n'.join([
            '[Interface]', f'PrivateKey = {private}', f'Address = {address}/32', '',
            '[Peer]', f"PublicKey = {state['cle_publique']}", f'PresharedKey = {shared}',
            f"Endpoint = {endpoint}:{state['port']}",
            f'AllowedIPs = {SERVER_ADDRESS}/32',      # seul le PC passe par le tunnel, rien d'autre
            'PersistentKeepalive = 25', ''])

    def remove_device(self, name: str) -> bool:
        state = self.state()
        if state is None:
            return False
        kept = [d for d in state['appareils'] if d['nom'] != name]
        if len(kept) == len(state['appareils']):
            return False
        state['appareils'] = kept
        self._save(state)
        return True

    def qr(self, config: str) -> str | None:
        """Le réglage en code QR pour le terminal, ou None si `qrencode` manque."""
        try:
            code, out = self._run(['qrencode', '-t', 'ansiutf8', '-m', '1'], config.encode())
        except OllamaError:
            return None
        return out.decode('utf-8', 'replace') if code == 0 and out else None

    def describe(self) -> list[str]:
        state = self.state()
        if state is None:
            return ['Tunnel privé : pas préparé (nexus vpn installer ADRESSE_PUBLIQUE).']
        lines = [f"Tunnel privé : préparé. Le PC y a l'adresse {SERVER_ADDRESS} ; il écoute sur "
                 f"{state['adresse_publique']}, port UDP {state['port']}."]
        lines += [f"  {d['adresse']:12} {d['nom']}" for d in state['appareils']] or ['  aucun appareil (nexus vpn ajouter "Mon téléphone")']
        return lines
