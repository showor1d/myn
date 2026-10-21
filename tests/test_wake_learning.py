"""Feature persistence and outcome gates, with isolated files and model decisions."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE/'src'), str(BASE/'src/core')]
import wake_learning as wl
import stt


def sample(seed):
    return [[(seed+i+j)/100 for j in range(13)] for i in range(30)]


class PersonalWake(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='myn-personal-wake-')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.now = 100.0
        self.trial = wl.Trial(clock=lambda: self.now)

    def begin(self, text='帮我看看明天的安排', ident='capture-a'):
        self.trial.begin(dict(capture_id=ident, wake_template=sample(5)))
        self.trial.observe(text)
        return self.trial.context()

    def decision(self, context, outcome='success', **changes):
        decision = dict(id=context['id'], revision=context['revision'], outcome=outcome,
                        scope='current', certainty='clear', evidence=context['question'],
                        reason='当前语义与这一轮唤醒相符')
        decision.update(changes)
        return decision

    def feedback(self, context, outcome='success', replied=True, **changes):
        return self.trial.feedback(context, self.decision(context, outcome, **changes),
                                   context['question'], replied, self.folder)

    def test_each_manual_sample_survives_reopen_before_three(self):
        for i in range(3):
            wl.enroll(self.folder, sample(i))
            self.assertEqual(len(wl.load(self.folder)['templates']), i+1)
            self.assertEqual(len(stt.load_wake_voice(self.folder)['templates']), 3 if i==2 else 0)
        self.assertNotIn('audio', (self.folder/wl.FILE).read_text())

    def test_saved_sensitivity_does_not_replace_or_clear_features(self):
        for i in range(4):
            wl.enroll(self.folder, sample(i))
        before = wl.load(self.folder)['templates']
        wl.sensitivity(self.folder, 'low')
        self.assertEqual(wl.active(self.folder), dict(templates=before, sensitivity='low'))

    def test_adaptation_keeps_manual_anchors_and_deduplicates_receipts(self):
        for i in range(4):
            wl.enroll(self.folder, sample(i))
        for i in range(10, 20):
            self.assertTrue(wl.adapt(self.folder, str(i), sample(i)))
        data = wl.load(self.folder)
        self.assertEqual(data['anchors'], [sample(i) for i in range(4)])
        self.assertEqual(len(data['adaptive']), 5)
        self.assertEqual(len(data['templates']), 8)
        self.assertFalse(wl.adapt(self.folder, '19', sample(19)))

    def test_corrupt_primary_recovers_good_copy_without_erasing_bad_file(self):
        for i in range(4):
            wl.enroll(self.folder, sample(i))
        path = self.folder/wl.FILE
        path.write_text('{bad', encoding='utf-8')
        self.assertEqual(len(wl.active(self.folder)['templates']), 3)
        self.assertEqual(path.read_text(), '{bad')
        path.with_suffix('.last-good.json').write_text('{bad')
        with self.assertRaises(ValueError):
            wl.enroll(self.folder, sample(6))
        self.assertEqual(path.read_text(), '{bad')

    def test_disk_failure_keeps_previous_feature_file(self):
        wl.enroll(self.folder, sample(1))
        before = (self.folder/wl.FILE).read_bytes()
        with patch.object(wl.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                wl.enroll(self.folder, sample(2))
        self.assertEqual((self.folder/wl.FILE).read_bytes(), before)

    def test_normal_chat_learns_only_after_reply_and_grace(self):
        context = self.begin()
        self.feedback(context, replied=False)
        self.now += 31
        self.assertFalse(self.trial.finish(self.folder))
        self.feedback(context)
        self.assertFalse(self.trial.finish(self.folder))
        self.now += 31
        self.assertTrue(self.trial.finish(self.folder))
        self.assertFalse(self.trial.finish(self.folder))
        self.assertEqual(wl.load(self.folder)['adaptive'][0]['id'], 'capture-a')

    def test_correction_before_commit_excludes_sample(self):
        context = self.begin()
        self.feedback(context)
        self.trial.observe('刚才是在跟室友讲话，没有叫你')
        context = self.trial.context()
        self.feedback(context, 'false')
        self.now += 60
        self.assertFalse(self.trial.finish(self.folder))
        self.assertEqual(wl.load(self.folder)['templates'], [])

    def test_late_correction_removes_only_this_trial(self):
        wl.enroll(self.folder, sample(1))
        wl.adapt(self.folder, 'prior', sample(2))
        context = self.begin()
        self.feedback(context)
        self.now += 31
        self.trial.finish(self.folder)
        self.trial.observe('不是在叫你，刚才那个是电视的声音')
        context = self.trial.context()
        self.assertTrue(self.feedback(context, 'false'))
        data = wl.load(self.folder)
        self.assertEqual([e['id'] for e in data['adaptive']], ['prior'])
        self.assertEqual(data['anchors'], [sample(1)])

    def test_new_speech_cancels_old_success_and_stale_callback(self):
        context = self.begin()
        self.feedback(context)
        self.trial.observe('你为什么突然答话')
        self.feedback(context)
        self.now += 31
        self.assertFalse(self.trial.finish(self.folder))
        self.trial.begin(dict(capture_id='capture-b', wake_template=sample(6)))
        self.feedback(context)
        self.now += 31
        self.assertFalse(self.trial.finish(self.folder))

    def test_invalid_uncertain_quoted_past_judgements_cannot_learn(self):
        for changes in (dict(certainty='uncertain'), dict(scope='keep'), dict(id='other'),
                        dict(revision=42), dict(evidence='不是当前原话'), dict(reason=''),
                        dict(outcome='uncertain')):
            context = self.begin('翻译“我没有叫你”这句话')
            decision = self.decision(context)
            decision.update(changes)
            self.trial.feedback(context, decision, context['question'], True, self.folder)
            self.now += 31
            self.assertFalse(self.trial.finish(self.folder), changes)

    def test_expiration_restart_and_unclear_segment_never_learn(self):
        context = self.begin()
        self.feedback(context)
        self.now += wl.TTL+1
        self.assertFalse(self.trial.finish(self.folder))
        self.assertIsNone(wl.Trial().context())
        self.begin()
        self.trial.begin(dict(capture_id='no-clean-segment'))
        self.assertIsNone(self.trial.context())

    def test_fixed_phrase_is_one_keyword_and_settings_keep_device(self):
        stt.save_settings(self.folder, stt.STTSettings(wake_words='旧名字', wake_learned='旧发音', device='Headset'))
        actual = stt.load_settings(self.folder)
        self.assertEqual(actual.wake_words, 'Hey Mine')
        self.assertEqual(actual.wake_learned, '')
        self.assertEqual(actual.device, 'Headset')
        self.assertEqual(stt.wake_word_list('Hey，Mine'), ['Hey Mine'])
        for text in ('Hey, Mine', 'hey mine', 'Hey，Mine！'):
            self.assertEqual(stt.strip_wake(text, actual.all_wake_words), '')
        for text in ('Hey', 'Mine', '这是我的 mine', 'Jarvis'):
            self.assertIsNone(stt.strip_wake(text, actual.all_wake_words))

    def test_scene_classifier_receives_trial_without_audio_and_reuses_request(self):
        import interaction, claude_client
        context = self.begin()
        answer = dict(wake_feedback=self.decision(context))
        with patch.object(claude_client, 'send', return_value=json.dumps(answer)) as call:
            result = interaction.classify(self.folder, {}, context['question'], [],
                                          dict(wake_trial=context), 'fake-key')
        self.assertEqual(call.call_count, 1)
        self.assertIn('wake_feedback', call.call_args.args[1])
        payload = json.loads(call.call_args.args[2])
        self.assertNotIn('template', payload['reading_panel']['wake_trial'])
        self.assertEqual(result, answer)

    def test_real_chat_path_waits_for_reply_and_reports_false_even_if_silent(self):
        import chat, claude_client
        session = chat.ChatSession(self.folder/'jarvis.db')
        session.interaction_notify = lambda: None
        context = self.begin()
        session.wake_context_provider = self.trial.context
        calls = []
        session.wake_feedback_notify = lambda *args: calls.append(args)
        scene = dict(mode='idle', scope='keep', certainty='clear', evidence=context['question'],
                     reason='当前请求', wake_feedback=self.decision(context))
        with patch.object(session, '_key', return_value='fake'), \
                patch.object(claude_client, 'send', return_value=json.dumps(scene)), \
                patch.object(claude_client, 'coordinated_reply', return_value='明天安排如下。'):
            result = session.ask(context['question'])
        self.assertTrue(result.get('reply_id'))
        self.assertEqual([c[-1] for c in calls], [False, True])
        self.trial.observe('刚才那个声音来自电视，我没叫你')
        context = self.trial.context()
        scene.update(wake_feedback=self.decision(context, 'false'),
                     addressing=dict(to_myn=False, ends=True, elsewhere=True))
        with patch.object(session, '_key', return_value='fake'), \
                patch.object(claude_client, 'send', return_value=json.dumps(scene)):
            session.inspect_voice(context['question'], [], [])
        self.assertEqual(calls[-1][1]['outcome'], 'false')
        self.assertFalse(calls[-1][-1])

    def test_training_window_autosaves_and_reopening_keeps_samples(self):
        from types import SimpleNamespace
        from gui_voice import VoiceMixin
        from web_panels import WakeTrainingPanel
        window = VoiceMixin()
        window.stt_settings = stt.STTSettings()
        window.wake_voice = dict(templates=[], sensitivity='medium')
        window._stt_job = lambda *a, **k: None
        window._show_error = lambda *a: self.fail(str(a))
        window.sheets = SimpleNamespace()
        with patch('gui_voice.user_data_dir', return_value=self.folder):
            for i in range(3):
                panel = WakeTrainingPanel(window, 'Hey Mine')
                panel.redraw = lambda: None
                panel.heard('Hey Mine', sample(i))
                panel.closed()  # no Save click
                self.assertEqual(len(wl.load(self.folder)['templates']), i+1)
        self.assertEqual(len(wl.active(self.folder)['templates']), 3)

    def test_gui_filters_echo_before_any_learning_and_observes_corrections(self):
        from test_voice_wake_ack import FakeWindow
        import time
        window = FakeWindow()
        window._judge_heard = lambda text: None  # addressing is exercised separately with a real session
        window.stt_settings = stt.STTSettings()
        window.wake_trial = self.trial
        window.last_speaking = time.time()
        window.recent_speech = [(time.time(), 'Hey Mine')]
        window.hear('Hey Mine', wake_template=sample(1))
        self.assertIsNone(self.trial.context())
        window.last_speaking = 0
        window.hear('Hey Mine', wake_template=sample(1))
        self.assertIsNotNone(self.trial.context())
        context = self.trial.context()
        window.hear('刚才不是在叫你')
        self.assertGreater(self.trial.context()['revision'], context['revision'])
        self.assertEqual(self.trial.context()['question'], '刚才不是在叫你')


if __name__ == '__main__':
    unittest.main()
