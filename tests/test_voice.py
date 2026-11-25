"""La voix : l'enregistrement est converti, transcrit puis effacé ; les silences ne deviennent pas des questions.

Le moteur de transcription (whisper.cpp) est simulé : ces tests portent sur la mécanique de NEXUS.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from nexus import voice as voice_module
from nexus.ollama import OllamaError
from nexus.voice import Voice, clean_transcript


class FakeEngines:
    def __init__(self, heard: str = ' Quand est mon prochain rendez-vous ?\n'):
        self.heard, self.calls, self.seen_files = heard, [], []
        self.convert_ok, self.wav_bytes = True, b'RIFF' + b'\0' * 20000

    def __call__(self, argv: list[str], timeout: int) -> tuple[int, bytes]:
        self.calls.append(argv)
        if argv[0] == 'ffmpeg':
            source = Path(argv[argv.index('-i') + 1])
            self.seen_files.append(source)
            self.received = source.read_bytes()
            if not self.convert_ok:
                return 1, b''
            Path(argv[-1]).write_bytes(self.wav_bytes)
            return 0, b''
        self.seen_files.append(Path(argv[argv.index('-f') + 1]))
        return 0, self.heard.encode()


class VoiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engines = FakeEngines()
        self.voice = Voice(Path(self.tmp.name) / 'voix', self.engines)

    def tearDown(self):
        self.tmp.cleanup()

    def test_recording_is_converted_transcribed_then_erased(self):
        self.assertEqual(self.voice.transcribe(b'\x1aE\xdf\xa3 faux webm'), 'Quand est mon prochain rendez-vous ?')
        convert, recognise = self.engines.calls
        self.assertEqual(convert[0], 'ffmpeg')
        self.assertIn('-nostdin', convert)
        self.assertEqual(convert[convert.index('-ar') + 1], '16000')
        self.assertEqual(convert[convert.index('-t') + 1], str(voice_module.MAX_SECONDS))     # durée bornée
        self.assertEqual(recognise[recognise.index('-l') + 1], 'fr')
        self.assertEqual(self.engines.received, b'\x1aE\xdf\xa3 faux webm')
        self.assertTrue(all(not path.exists() for path in self.engines.seen_files))           # rien n'est gardé

    def test_silence_and_ghost_sentences_are_not_questions(self):
        for heard in ('', '   \n', '[BLANK_AUDIO]', ' (silence) ', "Sous-titres réalisés par la communauté d'Amara.org",
                      "Merci d'avoir regardé cette vidéo !", '[Musique] '):
            self.engines.heard = heard
            self.assertEqual(self.voice.transcribe(b'x' * 100), '', heard)
        self.assertEqual(clean_transcript(' [Bruit]  Ajoute du lait   à ma liste. '), 'Ajoute du lait à ma liste.')
        self.assertEqual(len(clean_transcript('mot ' * 1000)), 1000)

    def test_bad_recordings_are_refused_with_a_clear_message(self):
        with self.assertRaises(OllamaError):
            self.voice.transcribe(b'')
        with self.assertRaises(OllamaError):
            self.voice.transcribe(b'x' * (voice_module.MAX_AUDIO + 1))
        self.engines.convert_ok = False
        with self.assertRaises(OllamaError) as caught:
            self.voice.transcribe(b'pas du son')
        self.assertIn('illisible ou trop court', str(caught.exception))
        self.engines.convert_ok, self.engines.wav_bytes = True, b'RIFF' + b'\0' * 100            # un dixième de seconde
        with self.assertRaises(OllamaError):
            self.voice.transcribe(b'clic')

    def test_not_installed_says_what_is_missing(self):
        real = Voice(Path(self.tmp.name) / 'absent')
        self.assertFalse(real.installed())
        self.assertIn('nexus voix installer', ' '.join(real.missing()))
        self.assertIn('pas prête', real.describe()[0])
        with self.assertRaises(OllamaError) as caught:
            real.transcribe(b'x' * 100)
        self.assertIn('pas installée', str(caught.exception))
        with self.assertRaises(OllamaError):
            real.install('enorme')

    def test_a_model_with_the_wrong_fingerprint_is_refused(self):
        target = Path(self.tmp.name) / 'modele.bin'
        source = Path(self.tmp.name) / 'faux.bin'
        source.write_bytes(b'pas le bon modele')

        class Response:
            def __init__(self):
                self.handle = source.open('rb')

            def geturl(self):
                return 'https://cdn.hf.co/x'

            def read(self, size):
                return self.handle.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.handle.close()
        original = voice_module.request.urlopen
        voice_module.request.urlopen = lambda *a, **k: Response()
        try:
            with self.assertRaises(OllamaError) as caught:
                Voice._download('https://huggingface.co/x', target, 'a' * 64)
            self.assertIn('empreinte', str(caught.exception))
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(self.tmp.name).glob('*.partiel')), [])
            import hashlib
            Voice._download('https://huggingface.co/x', target, hashlib.sha256(b'pas le bon modele').hexdigest())
            self.assertTrue(target.is_file())
        finally:
            voice_module.request.urlopen = original


if __name__ == '__main__':
    unittest.main()
