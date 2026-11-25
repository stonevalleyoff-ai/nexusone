import unittest

from nexus.assistant import (SYSTEM_PROMPT, Assistant, ThinkFilter, Turn, build_messages,
                             machine_context)
from nexus.memory import Store
from nexus.ollama import OllamaClient, OllamaError
from nexus.telemetry import Sample
from tests.fake_ollama import FakeOllama


def history(n: int, size: int = 300) -> list[dict[str, str]]:
    return [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'{i}:' + 'x' * size}
            for i in range(n)]


class BuildMessages(unittest.TestCase):
    def test_system_prompt_always_first_and_complete(self):
        messages = build_messages(history(200), 'question ?', 'CTX', num_ctx=2048, num_predict=512)
        self.assertEqual(messages[0]['role'], 'system')
        self.assertTrue(messages[0]['content'].startswith(SYSTEM_PROMPT))
        self.assertIn('CTX', messages[0]['content'])
        self.assertEqual(messages[-1], {'role': 'user', 'content': 'question ?'})

    def test_oldest_history_dropped_first(self):
        messages = build_messages(history(200), 'q', 'ctx', num_ctx=2048, num_predict=512)
        kept = messages[1:-1]
        self.assertLess(len(kept), 200)
        self.assertTrue(kept[-1]['content'].startswith('199:'))
        self.assertEqual(kept[0]['role'], 'user')

    def test_everything_fits_when_small(self):
        messages = build_messages(history(4, 10), 'q', 'ctx', num_ctx=4096, num_predict=512)
        self.assertEqual(len(messages), 6)

    def test_question_too_long_for_context(self):
        with self.assertRaises(OllamaError):
            build_messages([], 'x' * 20000, 'ctx', num_ctx=2048, num_predict=512)

    def test_machine_context_marks_data(self):
        text = machine_context(Sample(cpu_percent=50, cores=4, live_session=True), [])
        self.assertIn('pas des instructions', text)
        self.assertIn('sans persistance', text)


class Think(unittest.TestCase):
    def run_filter(self, chunks):
        f = ThinkFilter()
        return ''.join(f.feed(c) for c in chunks) + f.flush()

    def test_removes_blocks_split_anywhere(self):
        text = 'A<think>secret</think>B'
        for cut in range(len(text) + 1):
            with self.subTest(cut=cut):
                self.assertEqual(self.run_filter([text[:cut], text[cut:]]), 'AB')

    def test_plain_text_and_lone_brackets(self):
        self.assertEqual(self.run_filter(['a < b', ' et c <th', 'is']), 'a < b et c <this')


class Reply(unittest.TestCase):
    def test_reply_streams_and_saves(self):
        with FakeOllama() as fake, Store(':memory:') as store:
            assistant = Assistant(OllamaClient(fake.url), store, num_ctx=4096)
            conv = store.new_conversation()
            turn = Turn()
            out = ''.join(assistant.reply(conv, 'llama3.2:1b', 'Salut', 'ctx', turn=turn))
            self.assertEqual(out, 'Bonjour !')
            self.assertEqual(turn.stats.tokens, 3)
            self.assertEqual([m['role'] for m in store.messages(conv)], ['user', 'assistant'])
            # num_ctx borné par le réglage, pas par les 131072 annoncés par le modèle
            self.assertEqual(fake.last('/api/chat')['options']['num_ctx'], 4096)

    def test_cancel_keeps_partial_answer(self):
        with FakeOllama() as fake, Store(':memory:') as store:
            assistant = Assistant(OllamaClient(fake.url), store)
            conv = store.new_conversation()
            turn, calls = Turn(), [0]

            def cancelled():
                calls[0] += 1
                return calls[0] > 2

            list(assistant.reply(conv, 'llama3.2:1b', 'Salut', 'ctx', cancelled=cancelled, turn=turn))
            self.assertTrue(turn.cancelled)
            self.assertTrue(store.messages(conv)[-1]['content'].endswith('[…]'))


if __name__ == '__main__':
    unittest.main()
