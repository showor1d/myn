"""English presentation must preserve multilingual input and private source text."""
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core')]
import agenda
import agenda_display
import audio_inputs
import chat
import evolution
import interaction
import storage
import usage_stats
import usage_window
from gui_common import greeting_for_hour
from gui_voice import VoiceMixin
from web_panels3 import parse_tokens


class EnglishInterface(unittest.TestCase):
    def test_help_and_static_frontend_are_english(self):
        self.assertIsNone(re.search(r'[\u3400-\u9fff]', chat.HELP))
        for path in (BASE / 'src/web').iterdir():
            if path.suffix in ('.html', '.js'):
                self.assertIsNone(re.search(r'[\u3400-\u9fff]', path.read_text(encoding='utf-8')), path.name)

    def test_greetings_and_weekdays_are_english_without_rewriting_titles(self):
        self.assertEqual([greeting_for_hour(h) for h in (8, 13, 21)],
                         ['Good morning', 'Good afternoon', 'Good evening'])
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder)
            agenda.add(config, title='周一中文课程', start='2026-10-26 09:00')
            view = agenda_display.snapshot(config, '2026-10-26', '2026-10-26')
            self.assertEqual(view['days'][0]['weekday'], 'Mon')
            self.assertEqual(view['days'][0]['events'][0]['title'], '周一中文课程')

    def test_chinese_save_command_remains_local(self):
        self.assertIn('保存', chat.LOCAL_COMMANDS)
        self.assertEqual(chat._explicit_save('保存：保留中文原文'), '保留中文原文')

    def test_chinese_short_acknowledgements_do_not_end_study(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder)
            interaction.observe(config, '我要学习了')
            for phrase in ('好', '好的', '懂了', '继续'):
                context = interaction.observe(config, phrase)
                changed = interaction.decide(config, context,
                    dict(mode='idle', certainty='clear', evidence=phrase), phrase)
                self.assertFalse(changed, phrase)
            self.assertEqual(interaction.load(config)['mode'], 'study')

    def test_cosmetic_chinese_title_suffix_and_opposites(self):
        self.assertTrue(evolution._same_title('开启自动朗读', '开启自动朗读功能'))
        self.assertFalse(evolution._same_title('开启自动朗读', '关闭自动朗读'))

    def test_default_microphone_accepts_both_languages(self):
        for name in ('系统默认麦克风', 'System default microphone'):
            with tempfile.TemporaryDirectory() as folder, patch.object(audio_inputs, 'inventory',
                    return_value={'devices': ['Example microphone']}):
                client = Mock()
                client.audio_status.return_value = {'active': False, 'mode': 'inactive'}
                settings, result = audio_inputs.switch(Path(folder), client, name)
                self.assertEqual(settings.device, '')
                self.assertEqual(result['selected_device'], 'System default microphone')
                client.switch_microphone.assert_not_called()

    def test_english_counts_round_trip_and_chinese_units_remain_valid(self):
        for number, label in ((999, '999'), (1500, '1.5K'), (2000000, '2.0M'), (3000000000, '3.0B')):
            self.assertEqual(usage_window.short_tokens(number), label)
            self.assertEqual(usage_stats.tokens_text(number), label)
            self.assertEqual(parse_tokens(label), number)
        self.assertEqual(parse_tokens('2万'), 20000)
        self.assertEqual(parse_tokens('1亿'), 100000000)

    def test_both_inference_markers_stay_out_of_factual_queries(self):
        import sqlite3
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE records (metadata TEXT, raw_text TEXT)')
            for text in ('（推断）旧的推断', '(Inference) New inference', '原始事实'):
                db.execute('INSERT INTO records VALUES (?, ?)', ('{}', text))
            rows = db.execute('SELECT raw_text FROM records WHERE ' + storage.FACTUAL_MEMORY).fetchall()
            self.assertEqual(rows, [('原始事实',)])

    def test_new_and_legacy_wake_acknowledgements_are_echoes(self):
        window = VoiceMixin()
        for text in VoiceMixin.WAKE_ACKS + ('我在。', '在呢。', '嗯，我在听。'):
            self.assertTrue(window._is_wake_ack(text), text)
        self.assertFalse(window._is_wake_ack('请告诉我明天的天气'))


if __name__ == '__main__':
    unittest.main()
