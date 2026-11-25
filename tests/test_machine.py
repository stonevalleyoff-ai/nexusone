import os
from pathlib import Path
import tempfile
import unittest

from nexus.memory import Store
from nexus.telemetry import Sample, Telemetry, describe, parse_cpu_times, parse_meminfo
from nexus.watch import DEFAULT_RULES, Watcher, load_rules, validate_rule


class FakeStat:
    f_blocks, f_frsize, f_bavail = 1000, 1024 * 1024, 50


def fake_root(tmp: Path, cpu_line: str) -> tuple[Path, Path]:
    proc, sys = tmp / 'proc', tmp / 'sys'
    proc.mkdir()
    (proc / 'meminfo').write_text('MemTotal: 8388608 kB\nMemFree: 1 kB\nMemAvailable: 2097152 kB\n')
    (proc / 'stat').write_text(cpu_line + '\ncpu0 1 2 3 4\n')
    (proc / 'loadavg').write_text('0.50 0.40 0.30 1/100 42\n')
    (proc / 'uptime').write_text('3725.5 100.0\n')
    zone = sys / 'class/thermal/thermal_zone0'
    zone.mkdir(parents=True)
    (zone / 'temp').write_text('61500\n')
    bad = sys / 'class/thermal/thermal_zone1'
    bad.mkdir()
    (bad / 'temp').write_text('-273000\n')
    bat = sys / 'class/power_supply/BAT0'
    bat.mkdir(parents=True)
    (bat / 'type').write_text('Battery\n')
    (bat / 'capacity').write_text('42\n')
    (bat / 'status').write_text('Discharging\n')
    return proc, sys


class TelemetryTests(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(parse_meminfo('MemTotal: 2048 kB\nMemAvailable: 1024 kB'), (2, 1))
        self.assertEqual(parse_meminfo('MemTotal: abc kB'), (None, None))
        self.assertEqual(parse_cpu_times('cpu  10 0 10 70 10 0 0 0 0 0'), (100, 80))
        self.assertIsNone(parse_cpu_times('intr 1 2'))

    def test_full_sample_from_fake_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, sys = fake_root(Path(tmp), 'cpu  100 0 100 700 100 0 0 0 0 0')
            t = Telemetry(proc, sys, live_marker=Path(tmp) / 'live', persistence_dir=Path(tmp) / 'p',
                          statvfs=lambda _: FakeStat(), cores=lambda: 4)
            first = t.sample()
            self.assertIsNone(first.cpu_percent)  # il faut deux relevés
            (proc / 'stat').write_text('cpu  150 0 150 750 150 0 0 0 0 0\n')
            s = t.sample()
        self.assertAlmostEqual(s.cpu_percent, 50.0)
        self.assertEqual((s.mem_total_mib, s.mem_available_mib), (8192, 2048))
        self.assertAlmostEqual(s.mem_available_percent, 25.0)
        self.assertEqual((s.disk_total_mib, s.disk_free_mib), (1000, 50))
        self.assertEqual(s.temp_c, 61.5)
        self.assertEqual((s.battery_percent, s.on_battery), (42, True))
        self.assertFalse(s.live_session)
        text = '\n'.join(describe(s))
        self.assertIn('Processeur : 50 %', text)
        self.assertIn('Allumé depuis : 1 h 02 min', text)

    def test_missing_system_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            def no_disk(_):
                raise OSError
            s = Telemetry(Path(tmp) / 'none', Path(tmp) / 'none', statvfs=no_disk,
                          cores=lambda: None).sample()
        self.assertEqual(s, Sample())
        self.assertEqual(describe(s), [])

    def test_real_machine_does_not_crash(self):
        t = Telemetry()
        t.sample()
        self.assertIsInstance(describe(t.sample()), list)


class WatchTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(len(load_rules(DEFAULT_RULES)), len(DEFAULT_RULES))

    def test_sustain_and_hysteresis(self):
        rule = validate_rule({'id': 'mem', 'metric': 'mem_available_percent', 'op': 'below',
                              'threshold': 10, 'clear': 20, 'sustain': 2, 'level': 'critical',
                              'message': '{value:.0f} % libre'})
        w = Watcher([rule])
        low, mid, high = (Sample(mem_total_mib=100, mem_available_mib=v) for v in (5, 15, 30))
        self.assertEqual(w.update(low), [])                 # 1er relevé : pas encore
        [change] = w.update(low)                            # 2e : déclenché
        self.assertEqual((change.kind, change.alert.text), ('raised', '5 % libre'))
        self.assertEqual(w.update(mid), [])                 # 15 % : toujours actif (hystérésis)
        self.assertEqual(len(w.active()), 1)
        self.assertEqual(w.update(Sample()), [])            # mesure absente : rien ne change
        self.assertEqual(w.update(high)[0].kind, 'cleared')
        self.assertEqual(w.active(), [])

    def test_battery_only_when_discharging(self):
        w = Watcher(load_rules(DEFAULT_RULES))
        self.assertEqual(w.update(Sample(battery_percent=5, on_battery=False)), [])
        self.assertEqual(w.update(Sample(battery_percent=5, on_battery=True))[0].alert.rule.id,
                         'batterie-faible')

    def test_invalid_rules(self):
        base = {'id': 'r', 'metric': 'temp_c', 'op': 'above', 'threshold': 80, 'message': 'm'}
        for patch in ({'metric': 'rm -rf'}, {'op': 'gt'}, {'clear': 90}, {'sustain': 0},
                      {'level': 'panic'}, {'message': '{os.system}'}, {'exec': 'x'},
                      {'id': 'a b'}, {'threshold': True}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_rule({**base, **patch})
        with self.assertRaises(ValueError):
            load_rules([base, base])


class StoreTests(unittest.TestCase):
    def test_conversations_roundtrip_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sub' / 'n.sqlite3'
            with Store(path) as store:
                a = store.new_conversation()
                store.add(a, 'user', 'Comment va   la machine ?', 'm')
                store.add(a, 'assistant', 'Bien.', 'm')
                b = store.new_conversation()
                self.assertEqual(store.conversations()[-1]['title'], 'Comment va la machine ?')
                self.assertEqual(len(store.messages(a)), 2)
                self.assertTrue(store.delete(a))
                self.assertEqual(store.messages(a), [])
                with self.assertRaises(ValueError):
                    store.add(a, 'user', 'x', 'm')
                with self.assertRaises(ValueError):
                    store.add(b, 'system', 'x', 'm')
                store.add(b, 'user', 'secret-unique-42', 'm')
                self.assertEqual(store.delete_all(), 1)
            self.assertNotIn(b'secret-unique-42', path.read_bytes())


if __name__ == '__main__':
    unittest.main()
