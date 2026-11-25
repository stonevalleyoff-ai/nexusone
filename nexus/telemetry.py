"""Mesures de la machine, en lecture seule, depuis /proc et /sys (Linux).

Ce module n'ouvre aucun périphérique brut, ne lance aucune commande et n'écrit
nulle part. Les chemins sont injectables pour les tests.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Callable


@dataclass(frozen=True)
class Sample:
    cores: int | None = None
    cpu_percent: float | None = None
    load1: float | None = None
    mem_total_mib: int | None = None
    mem_available_mib: int | None = None
    disk_total_mib: int | None = None
    disk_free_mib: int | None = None
    temp_c: float | None = None
    battery_percent: int | None = None
    on_battery: bool | None = None
    uptime_s: float | None = None
    live_session: bool = False
    persistent: bool = False

    @property
    def mem_available_percent(self) -> float | None:
        if self.mem_total_mib and self.mem_available_mib is not None:
            return 100.0 * self.mem_available_mib / self.mem_total_mib
        return None

    @property
    def mem_used_percent(self) -> float | None:
        free = self.mem_available_percent
        return None if free is None else 100.0 - free

    @property
    def disk_free_percent(self) -> float | None:
        if self.disk_total_mib and self.disk_free_mib is not None:
            return 100.0 * self.disk_free_mib / self.disk_total_mib
        return None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding='ascii', errors='replace')
    except OSError:
        return None


def parse_meminfo(text: str) -> tuple[int | None, int | None]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(':')
        parts = rest.split()
        if key in ('MemTotal', 'MemAvailable') and len(parts) == 2 and parts[1] == 'kB':
            try:
                values[key] = int(parts[0]) // 1024
            except ValueError:
                pass
    return values.get('MemTotal'), values.get('MemAvailable')


def parse_cpu_times(text: str) -> tuple[int, int] | None:
    """(total, inactif) depuis la première ligne « cpu » de /proc/stat."""
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] == 'cpu':
            try:
                numbers = [int(x) for x in fields[1:9]]
            except ValueError:
                return None
            if len(numbers) < 5:
                return None
            idle = numbers[3] + numbers[4]  # idle + iowait
            return sum(numbers), idle
    return None


class Telemetry:
    def __init__(self, proc: Path = Path('/proc'), sys: Path = Path('/sys'),
                 disk: str = '/', live_marker: Path = Path('/run/live/medium'),
                 persistence_dir: Path = Path('/run/live/persistence'),
                 statvfs: Callable[[str], os.statvfs_result] = os.statvfs,
                 cores: Callable[[], int | None] = os.cpu_count):
        self.proc, self.sys, self.disk = proc, sys, disk
        self.live_marker = live_marker
        self.persistence_dir = persistence_dir
        self._statvfs, self._cores = statvfs, cores
        self._last_cpu: tuple[int, int] | None = None
        self._last_cpu_percent: float | None = None
        self._last_time = 0.0

    def _cpu_percent(self) -> float | None:
        text = _read(self.proc / 'stat')
        current = parse_cpu_times(text) if text else None
        if current is None:
            return None
        previous, self._last_cpu = self._last_cpu, current
        if previous is None:
            return None
        total, idle = current[0] - previous[0], current[1] - previous[1]
        if total <= 0:
            return self._last_cpu_percent
        self._last_cpu_percent = max(0.0, min(100.0, 100.0 * (total - idle) / total))
        return self._last_cpu_percent

    def _temperature(self) -> float | None:
        best = None
        try:
            zones = sorted((self.sys / 'class/thermal').glob('thermal_zone*'))
        except OSError:
            return None
        for zone in zones:
            raw = _read(zone / 'temp')
            try:
                value = int(raw.strip()) / 1000 if raw else None
            except ValueError:
                continue
            if value is not None and 0 < value < 130:
                best = value if best is None else max(best, value)
        return best

    def _battery(self) -> tuple[int | None, bool | None]:
        try:
            supplies = sorted((self.sys / 'class/power_supply').iterdir())
        except OSError:
            return None, None
        for supply in supplies:
            if (_read(supply / 'type') or '').strip() != 'Battery':
                continue
            raw = (_read(supply / 'capacity') or '').strip()
            status = (_read(supply / 'status') or '').strip()
            if raw.isdigit() and 0 <= int(raw) <= 100:
                return int(raw), status == 'Discharging'
        return None, None

    def sample(self) -> Sample:
        self._last_time = time.monotonic()
        total = available = None
        text = _read(self.proc / 'meminfo')
        if text:
            total, available = parse_meminfo(text)
        load1 = None
        text = _read(self.proc / 'loadavg')
        if text:
            try:
                load1 = float(text.split()[0])
            except (ValueError, IndexError):
                pass
        uptime = None
        text = _read(self.proc / 'uptime')
        if text:
            try:
                uptime = float(text.split()[0])
            except (ValueError, IndexError):
                pass
        disk_total = disk_free = None
        try:
            st = self._statvfs(self.disk)
            disk_total = st.f_blocks * st.f_frsize // 2**20
            disk_free = st.f_bavail * st.f_frsize // 2**20
        except OSError:
            pass
        battery, on_battery = self._battery()
        count = self._cores()
        return Sample(
            cores=count if isinstance(count, int) and count > 0 else None,
            cpu_percent=self._cpu_percent(), load1=load1,
            mem_total_mib=total, mem_available_mib=available,
            disk_total_mib=disk_total or None, disk_free_mib=disk_free,
            temp_c=self._temperature(), battery_percent=battery, on_battery=on_battery,
            uptime_s=uptime, live_session=self.live_marker.exists(),
            persistent=self._persistent())

    def _persistent(self) -> bool:
        try:
            return any(self.persistence_dir.iterdir())
        except OSError:
            return False


def gib(mib: int | None) -> str:
    return '?' if mib is None else f'{mib / 1024:.1f} Go'


def describe(sample: Sample) -> list[str]:
    """Résumé en lignes courtes, pour l'assistant et la console."""
    lines = []
    if sample.cpu_percent is not None:
        lines.append(f'Processeur : {sample.cpu_percent:.0f} % utilisé'
                     + (f' sur {sample.cores} cœurs' if sample.cores else ''))
    if sample.mem_total_mib:
        lines.append(f'Mémoire : {gib(sample.mem_available_mib)} libres sur {gib(sample.mem_total_mib)}')
    if sample.disk_total_mib:
        if sample.live_session and not sample.persistent:
            label = "Espace de session (en RAM, perdu à l'arrêt)"
        elif sample.live_session:
            label = 'Espace de session (persistance USB)'
        else:
            label = 'Disque système'
        lines.append(f'{label} : {gib(sample.disk_free_mib)} libres sur {gib(sample.disk_total_mib)}')
    if sample.temp_c is not None:
        lines.append(f'Température maximale : {sample.temp_c:.0f} °C')
    if sample.battery_percent is not None:
        lines.append(f'Batterie : {sample.battery_percent} %'
                     + (' (sur batterie)' if sample.on_battery else ' (sur secteur)'))
    if sample.uptime_s is not None:
        h, m = divmod(int(sample.uptime_s) // 60, 60)
        lines.append(f'Allumé depuis : {h} h {m:02d} min' if h else f'Allumé depuis : {m} min')
    return lines
