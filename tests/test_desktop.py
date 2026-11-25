"""Test de fumée de l'interface : ignoré sans tkinter ou sans écran (DISPLAY)."""
import gc
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None

from nexus.config import Settings
from tests.fake_ollama import FakeOllama


@unittest.skipUnless(tk is not None and os.environ.get('DISPLAY'), 'tkinter ou DISPLAY absent')
class DesktopSmoke(unittest.TestCase):
    def pump(self, root, until, seconds=10.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            root.update()
            if until():
                return True
            time.sleep(0.02)
        return False

    def test_connect_chat_and_forget(self):
        from nexus import desktop
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            settings = Settings(ollama_url=fake.url, model='qwen3:4b',
                                db_path=Path(tmp) / 'db.sqlite3', rules_path=Path(tmp) / 'none.json',
                                num_ctx=4096)
            root = tk.Tk()
            app = None
            try:
                app = desktop.NexusDesktop(root, settings)
                self.assertTrue(self.pump(root, lambda: app.status.cget('text').startswith('Prêt')))
                self.assertEqual(app.model.get(), 'qwen3:4b')
                self.assertEqual(list(app.model_box['values']), ['Automatique', 'llama3.2:1b', 'qwen3:4b'])
                app.entry.insert('1.0', 'Bonjour ?')
                app.send()
                self.assertTrue(self.pump(root, lambda: not app.generating))
                text = app.transcript.get('1.0', 'end')
                self.assertIn('Bonjour !', text)
                self.assertIn('3 jetons', text)
                self.assertEqual(len(app.history_ids), 1)
                app.store.delete_all()
                app.refresh_conversations()
                self.assertEqual(app.history_ids, [])
            finally:
                # Laisser finir les fils de l'interface avant de détruire Tk (sinon Tcl abandonne).
                for thread in threading.enumerate():
                    if thread.name.startswith('nexus-'):
                        thread.join(10)
                root.destroy()
                # Libérer les objets Tk ici, dans le fil principal : sinon le ramasse-miettes
                # peut le faire depuis un fil du faux serveur, et Tcl abandonne le processus.
                app = None
                gc.collect()

    def test_the_window_starts_the_engine_and_stops_it_on_close(self):
        """colibrì tenu par NEXUS : ouvrir la fenêtre suffit, la fermer libère la mémoire."""
        from nexus import desktop
        from tests.test_engine import make_colibri
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            code, model = make_colibri(Path(tmp))
            settings = Settings(ollama_url=fake.url, model=None, db_path=Path(tmp) / 'db.sqlite3',
                                rules_path=Path(tmp) / 'none.json', num_ctx=8192, backend='colibri',
                                engine_dir=Path(tmp) / 'moteur')
            root = tk.Tk()
            app = engine = None
            statuses: list[str] = []
            try:
                with mock.patch.dict(os.environ, {'NEXUS_COLIBRI_DIR': str(code), 'NEXUS_COLIBRI_MODELE': str(model)}):
                    app = desktop.NexusDesktop(root, settings)
                engine = app.client.engine
                self.assertFalse(engine.running())
                self.assertTrue(self.pump(root, lambda: statuses.append(app.status.cget('text'))
                                          or app.status.cget('text').startswith('Prêt'), 20))
                self.assertTrue(any('Démarrage du moteur' in text for text in statuses), statuses[-3:])
                self.assertEqual(app.model.get(), 'Automatique')          # rien d'imposé : NEXUS choisit
                self.assertEqual(app.status.cget('text'), 'Prêt · automatique (gros-modele)')
                app.entry.insert('1.0', 'Bonjour ?')
                app.send()
                self.assertTrue(self.pump(root, lambda: not app.generating))
                self.assertIn('Bonjour du moteur', app.transcript.get('1.0', 'end'))
                self.assertTrue(engine.running())
                app.close()
                for thread in threading.enumerate():
                    if thread.name.startswith('nexus-'):
                        thread.join(15)
                self.assertFalse(engine.running())
            finally:
                for thread in threading.enumerate():
                    if thread.name.startswith('nexus-'):
                        thread.join(10)
                try:
                    root.destroy()
                except tk.TclError:
                    pass   # déjà détruite par close()
                if engine is not None:
                    engine.stop(10)
                app = None
                gc.collect()


if __name__ == '__main__':
    unittest.main()
