"""Independent real-Store checks for quoted natural-language updates.

No model, browser, production data or network is used. Test fixtures are fictional.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desk_fixture import Desk


class ExistingUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Desk(Path(self.tmp.name) / 'data')
        self.d.settings['timezone'] = 'Asia/Shanghai'
        self.session = self.d.store.put('session', {'title': '测试用摄影讨论'})
        self.g = self.goal('摄影作品集', ['选三张照片', '写照片说明'])

    def tearDown(self):
        self.tmp.cleanup()

    def goal(self, title, actions):
        return self.d.goal({'title': title, 'status': 'active',
                            'steps': [{'text': a, 'done': False} for a in actions]})

    def item(self, change='complete', quote='选三张照片完成了', goal=None,
             index=0, key='u1', date_text=None, kind='update_action'):
        goal = goal or self.g
        result = {'key': key, 'kind': kind, 'target_goal': goal['id'],
                  'change': change, 'source_quote': quote}
        if kind == 'update_action':
            result['target_action'] = goal['steps'][index]['id']
        if date_text is not None:
            result['date_text'] = date_text
        return result

    def job(self, message, items, goals=None, contexts=True):
        goals = copy.deepcopy(goals if goals is not None else [self.g])
        return self.d.store.put('job', {
            'session_id': self.session['id'], 'message': message,
            'status': 'completed', 'organize': True, 'capture_items': items,
            # At this instant Shanghai is already Oct 8, not Oct 7.
            'created_at': '2026-10-07T18:30:00+00:00',
            'answer_context': {'contexts': ([{'id': 'active-goals', 'text': json.dumps(goals, ensure_ascii=False)}] if contexts else [])},
        })

    def apply(self, message, items, goals=None, contexts=True):
        job = self.job(message, items, goals, contexts)
        return job, self.d.organize_reply(job['id'])

    def current(self, goal=None):
        return self.d.store.get((goal or self.g)['id'], 'goal')

    def assertRejectedUnchanged(self, message, item, goal=None, goals=None, contexts=True):
        original = self.current(goal)
        _, result = self.apply(message, [item], goals, contexts)
        self.assertEqual(result['capture_status'], 'failed', message)
        self.assertEqual(self.current(goal), original, message)
        self.assertTrue(result.get('capture_error'))

    def test_complete_has_real_same_id_receipt_and_self_report_source(self):
        job, result = self.apply('选三张照片完成了', [self.item()])
        self.assertEqual(result['capture_status'], 'saved')
        current = self.current()
        self.assertEqual(current['id'], self.g['id'])
        self.assertEqual(current['rev'], self.g['rev'] + 1)
        self.assertTrue(current['steps'][0]['done'])
        self.assertEqual(current['steps'][0]['status'], 'done')
        self.assertEqual(current['steps'][0]['outcome'], '本人自报：选三张照片完成了')
        saved = result['capture_receipt']['saved_items'][0]
        self.assertEqual(saved['id'], self.g['steps'][0]['id'])
        self.assertEqual(saved['goal_id'], self.g['id'])
        self.assertTrue(saved['done'])
        self.assertEqual(len(self.d.store.all('goal')), 1)
        self.assertEqual(current['steps'][0]['capture']['source_id'], 'job:' + job['id'])

    def test_reopen_uses_current_snapshot_and_preserves_original_action_id(self):
        self.apply('选三张照片完成了', [self.item()])
        latest = self.current()
        quote = '选三张照片没完成，恢复待做'
        _, result = self.apply(quote, [self.item('reopen', quote, goal=latest)], [latest])
        self.assertEqual(result['capture_status'], 'saved')
        current = self.current()
        self.assertFalse(current['steps'][0]['done'])
        self.assertEqual(current['steps'][0]['id'], latest['steps'][0]['id'])
        self.assertEqual(current['steps'][0]['status'], 'active')
        self.assertIn('本人更正：', current['steps'][0]['outcome'])

    def test_relative_and_explicit_schedule_use_job_local_calendar_not_wall_clock(self):
        for date_text, expected in [('今天', '2026-10-08'), ('明天', '2026-10-09'), ('后天', '2026-10-10'), ('2026-12-31', '2026-12-31')]:
            with self.subTest(date_text=date_text):
                latest = self.current()
                quote = f'把选三张照片安排到{date_text}'
                _, result = self.apply(quote, [self.item('schedule', quote, goal=latest, date_text=date_text)], [latest])
                self.assertEqual(result['capture_status'], 'saved')
                self.assertEqual(self.current()['steps'][0]['due'], expected)
                self.assertFalse(self.current()['steps'][0]['done'])
        self.d.settings['timezone'] = 'UTC'
        latest = self.current()
        quote = '把选三张照片安排到今天'
        self.apply(quote, [self.item('schedule', quote, goal=latest, date_text='今天')], [latest])
        self.assertEqual(self.current()['steps'][0]['due'], '2026-10-07')

    def test_invalid_or_invented_date_does_not_write(self):
        for quote, date_text in [('把选三张照片安排到2026-02-30', '2026-02-30'), ('把选三张照片安排到明天', '2026-10-09'), ('把选三张照片安排到下周', '下周')]:
            with self.subTest(quote=quote):
                self.assertRejectedUnchanged(quote, self.item('schedule', quote, date_text=date_text))

    def test_goal_pause_resume_complete(self):
        for change, quote, status in [('pause', '暂停摄影作品集', 'paused'), ('resume', '继续摄影作品集', 'active'), ('complete', '摄影作品集完成了', 'done')]:
            with self.subTest(change=change):
                latest = self.current()
                _, result = self.apply(quote, [self.item(change, quote, latest, kind='update_goal')], [latest])
                self.assertEqual(result['capture_status'], 'saved')
                self.assertEqual(self.current()['status'], status)
        self.assertEqual(self.current()['evidence'], '本人自报：摄影作品集完成了')

    def test_negated_hypothetical_reported_or_question_intent_cannot_complete(self):
        examples = [
            ('选三张照片还没完成', '选三张照片还没完成'),
            ('如果选三张照片完成了，就继续', '选三张照片完成了'),
            ('他说选三张照片完成了', '选三张照片完成了'),
            ('选三张照片完成了吗？', '选三张照片完成了'),
            ('我没把选三张照片做完', '选三张照片做完'),
            ('选三张照片并非已完成', '选三张照片并非已完成'),
            ('选三张照片尚未达成', '选三张照片尚未达成'),
            ('不要把选三张照片标记完成', '选三张照片标记完成'),
            ('选三张照片完成了才可以继续', '选三张照片完成了'),
        ]
        for message, quote in examples:
            with self.subTest(message=message):
                self.g = self.current()
                self.assertRejectedUnchanged(message, self.item('complete', quote))

    def test_truncated_quote_cannot_remove_surrounding_negation(self):
        for message, quote in [
            ('不要把选三张照片标记完成', '选三张照片标记完成'),
            ('选三张照片完成了这句话不是真的', '选三张照片完成了'),
            ('选三张照片完成了？其实并没有', '选三张照片完成了'),
        ]:
            with self.subTest(message=message):
                self.g = self.current()
                self.assertRejectedUnchanged(message, self.item('complete', quote))

    def test_negated_reopen_schedule_pause_and_resume_cannot_write(self):
        examples = [
            ('reopen', '不要把选三张照片恢复待做', '选三张照片恢复待做', 'update_action', None),
            ('schedule', '暂时不把选三张照片安排到明天', '选三张照片安排到明天', 'update_action', '明天'),
            ('pause', '不要暂停摄影作品集', '暂停摄影作品集', 'update_goal', None),
            ('resume', '摄影作品集还没恢复', '摄影作品集还没恢复', 'update_goal', None),
        ]
        for change, message, quote, kind, date_text in examples:
            with self.subTest(message=message):
                self.g = self.current()
                self.assertRejectedUnchanged(message, self.item(change, quote, kind=kind, date_text=date_text))

    def test_same_goal_duplicate_action_name_is_rejected_even_with_goal_name(self):
        goal = self.goal('英语训练', ['练口语', '练口语'])
        self.assertRejectedUnchanged('英语训练的练口语完成了', self.item('complete', '英语训练的练口语完成了', goal), goal, [goal])

    def test_cross_goal_duplicate_action_needs_goal_name(self):
        other = self.goal('周末摄影', ['选三张照片'])
        self.assertRejectedUnchanged('选三张照片完成了', self.item(), goals=[self.g, other])
        quote = '摄影作品集的选三张照片完成了'
        _, result = self.apply(quote, [self.item('complete', quote)], [self.g, other])
        self.assertEqual(result['capture_status'], 'saved')
        self.assertTrue(self.current()['steps'][0]['done'])
        self.assertFalse(self.current(other)['steps'][0]['done'])

    def test_duplicate_goal_names_cannot_be_selected_by_model_id(self):
        other = self.goal(self.g['title'], ['另一行动'])
        self.assertRejectedUnchanged('暂停摄影作品集', self.item('pause', '暂停摄影作品集', kind='update_goal'), goals=[self.g, other])

    def test_missing_snapshot_foreign_action_and_unmatched_quote_are_rejected(self):
        self.assertRejectedUnchanged('选三张照片完成了', self.item(), contexts=False)
        other = self.goal('英语学习', ['背单词'])
        bad = self.item(); bad['target_action'] = other['steps'][0]['id']
        self.assertRejectedUnchanged('选三张照片完成了', bad)
        self.assertRejectedUnchanged('今天心情不错', self.item())
        self.assertRejectedUnchanged('我完成它了', self.item('complete', '我完成它了'))

    def test_same_batch_multiple_actions_then_goal_change_are_not_revision_conflicts(self):
        message = '选三张照片完成了。把写照片说明安排到明天。暂停摄影作品集。'
        items = [self.item(key='done'), self.item('schedule', '把写照片说明安排到明天', index=1, key='date', date_text='明天'), self.item('pause', '暂停摄影作品集', kind='update_goal', key='pause')]
        _, result = self.apply(message, items)
        self.assertEqual(result['capture_status'], 'saved')
        current = self.current()
        self.assertEqual(current['rev'], self.g['rev'] + 3)
        self.assertTrue(current['steps'][0]['done'])
        self.assertEqual(current['steps'][1]['due'], '2026-10-09')
        self.assertEqual(current['status'], 'paused')
        self.assertEqual(len(result['capture_receipt']['saved_items']), 3)

    def test_stale_revision_refuses_entire_batch_without_partial_business_write(self):
        self.d.goal({**self.g, 'why': '外部已修改'})
        before = self.current()
        message = '选三张照片完成了。把写照片说明安排到明天。'
        _, result = self.apply(message, [self.item(), self.item('schedule', '把写照片说明安排到明天', index=1, key='date', date_text='明天')])
        self.assertEqual(result['capture_status'], 'failed')
        self.assertEqual(self.current(), before)
        self.assertFalse(self.d.store.all('capture'))

    def test_pronoun_binding_cannot_mark_the_wrong_action(self):
        # A model-provided ID must not replace a missing unambiguous user instruction.
        for message in [
            '选三张照片和写照片说明，前者完成了',
            '选三张照片和写照片说明，后者完成了',
            '选三张照片和写照片说明，其中一个完成了',
        ]:
            with self.subTest(message=message):
                self.g = self.current()
                self.assertRejectedUnchanged(message, self.item('complete', message, index=1))

    def test_one_quote_cannot_transfer_another_actions_completion_or_date(self):
        examples = [
            ('选三张照片完成了，写照片说明安排到明天', 'complete', 1, None),
            ('选三张照片完成了，写照片说明安排到明天', 'schedule', 0, '明天'),
            ('把选三张照片安排到明天，写照片说明安排到后天', 'schedule', 0, '后天'),
        ]
        for message, change, index, date_text in examples:
            with self.subTest(message=message, change=change, index=index):
                self.g = self.current()
                self.assertRejectedUnchanged(message, self.item(change, message, index=index, date_text=date_text))

    def test_multi_goal_quote_cannot_transfer_other_goals_status(self):
        other = self.goal('英语训练', ['跟读一分钟'])
        message = '暂停摄影作品集，继续英语训练'
        self.assertRejectedUnchanged(message, self.item('pause', message, other, kind='update_goal'), other, [self.g, other])

    def test_explicit_bounded_quotes_allow_two_goals_and_two_different_dates(self):
        other = self.goal('英语训练', ['跟读一分钟'])
        message = '把选三张照片安排到明天。把写照片说明安排到后天。暂停摄影作品集。继续英语训练。'
        items = [
            self.item('schedule', '把选三张照片安排到明天', date_text='明天', key='date1'),
            self.item('schedule', '把写照片说明安排到后天', index=1, date_text='后天', key='date2'),
            self.item('pause', '暂停摄影作品集', kind='update_goal', key='pause'),
            self.item('resume', '继续英语训练', other, kind='update_goal', key='resume'),
        ]
        _, result = self.apply(message, items, [self.g, other])
        self.assertEqual(result['capture_status'], 'saved')
        self.assertEqual(self.current()['steps'][0]['due'], '2026-10-09')
        self.assertEqual(self.current()['steps'][1]['due'], '2026-10-10')
        self.assertEqual(self.current()['status'], 'paused')
        self.assertEqual(self.current(other)['status'], 'active')

    def test_stale_second_goal_preflight_leaves_first_goal_untouched(self):
        other = self.goal('英语训练', ['跟读一分钟'])
        self.d.goal({**other, 'why': '外部更新第二目标'})
        first_before, second_before = self.current(), self.current(other)
        message = '选三张照片完成了。继续英语训练。'
        items = [self.item(), self.item('resume', '继续英语训练', other, kind='update_goal', key='resume')]
        _, result = self.apply(message, items, [self.g, other])
        self.assertEqual(result['capture_status'], 'failed')
        self.assertEqual(self.current(), first_before)
        self.assertEqual(self.current(other), second_before)
        self.assertFalse(self.d.store.all('capture'))

    def test_user_opt_out_prevents_existing_update_even_with_valid_model_item(self):
        before = self.current()
        _, result = self.apply('选三张照片完成了，但只讨论，不要保存。', [self.item()])
        self.assertEqual(result['capture_status'], 'empty')
        self.assertEqual(self.current(), before)
        self.assertFalse(self.d.store.all('capture'))

    def test_saved_update_is_idempotent_across_repeat_and_process_restart(self):
        job, first = self.apply('选三张照片完成了', [self.item()])
        before = self.current()
        for desk in [self.d, Desk(self.d.store.path)]:
            result = desk.organize_reply(job['id'])
            self.assertEqual(result['capture_status'], 'saved')
            self.assertTrue(result['capture_receipt']['replayed'])
            self.assertEqual(result['capture_receipt']['id'], first['capture_receipt']['id'])
            self.assertEqual(desk.store.get(self.g['id'], 'goal'), before)
            self.assertEqual(len(desk.store.all('capture')), 1)
            self.assertEqual(len(desk.store.all('goal')), 1)


if __name__ == '__main__':
    unittest.main()
