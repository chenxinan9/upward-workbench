"""Independent user-behavior acceptance; only temporary data and stubbed turns."""
import copy
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import Desk
from phase import TZ


class GrowthContextAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / 'data'
        self.vault = self.root / 'vault'
        self.private = self.root / 'private'
        for folder in (self.data, self.vault, self.private):
            folder.mkdir()
        (self.vault / 'scope.md').write_text('# 合成当前文档\n此文档不授权修改个人全景。')
        self.today = dt.datetime.now(TZ).date().isoformat()
        self.profile = self.private / 'profile.md'
        self.profile.write_text('---\noccurred_at: "2020-01-01"\n---\n# 合成个人资料\n历史身高与技能记录。')
        self.plan_path = self.private / 'plan.json'
        self.initial_plan = {
            'schema_version': 1,
            'unknown_extension': {'preserve': ['keep', 7]},
            'source_register': [
                {'id': 'profile', 'title': '合成个人资料', 'root': 'private',
                 'path': 'profile.md', 'date': '2020-01-01',
                 'evidence_type': 'personal_profile',
                 'sha256': hashlib.sha256(self.profile.read_bytes()).hexdigest()}
            ],
            'domains': [{'id': 'business', 'title': '已登记方向', 'status': 'proposal'}],
            'current_focus_draft': {'title': '原今日重点', 'status': 'confirmed',
                                    'reason': '本人先前确认', 'keep_focus': 'untouched'},
            'life_panorama': {
                'north_star': {'quote': '原长期方向', 'status': 'confirmed',
                               'date': '2025-01-01', 'source_id': 'profile', 'keep_north': 'untouched'},
                'pillars': [{'title': '健康', 'desired_life': '长期有精力'}],
                'route': [{'horizon': '三年', 'title': '原路线'}],
                'history': [{'date': '2025-01-01', 'title': '旧方向'}],
                'unknown_panorama': {'preserve': True}
            }
        }
        self.plan_path.write_text(json.dumps(self.initial_plan, ensure_ascii=False))
        self.settings = {'growth_plan': str(self.plan_path), 'private_root': str(self.private),
                         'vault_roots': [{'id': 'upward', 'path': str(self.vault), 'writable': True}]}
        (self.data / 'settings.json').write_text(json.dumps(self.settings))
        self.desk = Desk(self.data)
        self.addCleanup(self.desk.stop_workspace)
        self.last_turn = None

    def plan(self):
        return json.loads(self.plan_path.read_text())

    def run_turn(self, message, callback, scope='personal', purpose='general', stop=None, expected_status='completed'):
        session = self.desk.store.put('workspace-session', {'title': '独立合成验收'})
        job = self.desk.store.put('workspace-job', {
            'session_id': session['id'], 'status': 'queued',
            'request_context': {'scope': scope, 'purpose': purpose, 'days': 7}
        })
        self.desk.store.put('workspace-message', {'session_id': session['id'], 'role': 'user',
                                                   'text': message, 'status': 'completed', 'job_id': job['id']})
        observed = {}

        def fake(client, **kw):
            observed.update(kw)
            try:
                callback(kw['tool_call'], kw)
            except AssertionError as error:
                observed['callback_failure'] = str(error)
                raise
            return {'text': '合成工具回执核对完成', 'status': 'cancelled' if stop and stop.is_set() else 'completed', 'actual_model': 'fixture-only'}

        with patch('codex_workspace.CodexWorkspace.run_turn', fake), \
             patch.object(self.desk, 'creator_evidence', return_value={
                 'status': 'not_configured', 'works': [], 'publications': [], 'warnings': []}):
            current = self.desk.vault.read('upward:scope.md') if scope == 'document' else None
            self.desk.workspace_run(job, session, {'id': 'fixture-only'}, message,
                                    current, '', False, stop or threading.Event())
        final = self.desk.store.get(job['id'], 'workspace-job')
        self.last_turn = final
        self.assertEqual(final['status'], expected_status, observed.get('callback_failure') or final.get('error'))
        return observed, final

    def update(self, tool, section, patch_data, read=None):
        read = read or tool('read_growth_plan', {})
        return tool('update_growth_plan', {
            'section': section, 'expected_sha256': read['sha256'],
            'content': json.dumps(patch_data, ensure_ascii=False)
        })

    def test_explicit_panorama_update_reads_back_and_preserves_unrelated_fields(self):
        before = copy.deepcopy(self.initial_plan)
        receipts = []
        def check(tool, kw):
            read = tool('read_growth_plan', {})
            self.assertEqual(read['document_id'], 'growth-plan')
            self.assertEqual(read['format'], 'json')
            self.assertEqual(read['life_panorama'], before['life_panorama'])
            receipts.append(self.update(tool, 'panorama', {'north_star': {'quote': '长期持续创作并照顾健康'}}, read))
        self.run_turn('把我的人生全景中的长期方向更新为“长期持续创作并照顾健康”，请保存。', check)
        result = self.plan()
        expected = copy.deepcopy(before['life_panorama'])
        expected['north_star']['quote'] = '长期持续创作并照顾健康'
        # Provenance/version fields may be appended; existing business fields must survive.
        for key, value in expected.items():
            if key == 'north_star':
                for field, item in value.items():
                    if field in ('date', 'source_id'):continue
                    self.assertEqual(result['life_panorama'][key][field], item)
            elif key == 'history':
                self.assertEqual(result['life_panorama'][key][:len(value)], value)
            else:
                self.assertEqual(result['life_panorama'][key], value)
        for key in ('current_focus_draft', 'domains', 'source_register', 'unknown_extension'):
            self.assertEqual(result[key], before[key])
        self.assertTrue(receipts[0]['verified'])
        self.assertTrue(receipts[0]['read_back'])
        self.assertEqual(receipts[0]['sha256'], hashlib.sha256(self.plan_path.read_bytes()).hexdigest())
        self.assertEqual(self.desk.growth_snapshot()['panorama']['north_star']['quote'], '长期持续创作并照顾健康')

    def test_new_north_star_has_current_utterance_basis_instead_of_old_source_attribution(self):
        statement = '把人生全景的长期方向更新为长期创作与规律训练，并保存。'
        def check(tool, kw):
            self.update(tool, 'panorama', {'north_star': {'quote': '长期创作与规律训练'}})
        _, final = self.run_turn(statement, check)
        north = self.desk.growth_snapshot()['panorama']['north_star']
        self.assertNotEqual(north.get('source_id'), 'profile', '新原话不能挂在旧资料出处下')
        self.assertNotEqual(north.get('date'), '2025-01-01', '新原话不能继承旧发生日期')
        basis = self.desk.source(north['source_id'])
        self.assertIn(statement, basis['text'])
        self.assertIn('长期创作与规律训练', basis['text'])
        source_ids = {row['id'] for row in self.desk.growth_snapshot()['sources']}
        self.assertIn(north['source_id'], source_ids, '页面必须能通过登记入口回读新陈述')
        history = self.desk.growth_snapshot()['panorama']['history']
        self.assertTrue(any(row.get('source_id') == 'profile' and row.get('date') == '2025-01-01'
                            for row in history), '旧出处和旧日期仅留在方向历史')
        revision = self.plan()['revisions'][-1]
        self.assertEqual(revision['job_id'], final['id'])
        witnesses = [m for m in self.desk.store.all('workspace-message')
                     if m.get('job_id') == revision['job_id'] and m.get('role') == 'user']
        self.assertEqual([m['text'] for m in witnesses], [statement])
        backups = self.data / 'growth-plan-history'
        originals = [json.loads(p.read_text()) for p in backups.glob('*.json')
                     if not p.name.endswith('.meta.json')]
        self.assertTrue(any(p.get('life_panorama', {}).get('north_star', {}).get('source_id') == 'profile'
                            for p in originals), '旧出处应作为可恢复历史保留')

    def test_daily_update_has_own_authority_and_never_changes_panorama(self):
        original = copy.deepcopy(self.initial_plan['life_panorama'])
        def check(tool, kw):
            result = self.update(tool, 'daily', {'title': '录一段真实产品演示', 'reason': '今天完成一次验证'})
            self.assertTrue(result['verified'])
            with self.assertRaises((PermissionError, ValueError)):
                self.update(tool, 'panorama', {'north_star': {'quote': '不该被改的方向'}})
        self.run_turn('不要改我的长期方向，只把今日重点改成录一段真实产品演示，原因是今天完成一次验证，请保存。', check)
        result = self.plan()
        self.assertEqual(result['life_panorama'], original)
        self.assertEqual(result['current_focus_draft']['title'], '录一段真实产品演示')
        self.assertEqual(result['current_focus_draft']['keep_focus'], 'untouched')
        self.assertEqual(self.desk.growth_snapshot()['today']['focus']['title'], '录一段真实产品演示')

    def test_common_daily_commands_allow_protective_negation_of_other_objects(self):
        cases = [('把今日重点设为拍摄演示，不修改其他内容。', '拍摄演示'),
                 ('不要改人生全景，只把今日重点改为训练半小时。', '训练半小时')]
        panorama = copy.deepcopy(self.plan()['life_panorama'])
        for message, title in cases:
            with self.subTest(message=message):
                def check(tool, kw):
                    result = self.update(tool, 'daily', {'title': title})
                    self.assertTrue(result['verified'])
                    with self.assertRaises((PermissionError, ValueError)):
                        self.update(tool, 'panorama', {'north_star': {'quote': '不能越权'}})
                self.run_turn(message, check)
                self.assertEqual(self.plan()['current_focus_draft']['title'], title)
                self.assertEqual(self.plan()['life_panorama'], panorama)

    def test_reconfirming_yesterdays_same_focus_advances_date_but_same_day_retry_is_noop(self):
        plan = self.plan()
        plan['current_focus_draft']['title'] = '训练半小时'
        plan['current_focus_draft']['date'] = (dt.datetime.now(TZ).date() - dt.timedelta(days=1)).isoformat()
        self.plan_path.write_text(json.dumps(plan, ensure_ascii=False))
        results = []
        def check(tool, kw):
            results.append(self.update(tool, 'daily', {'title': '训练半小时'}))
        self.run_turn('把今日重点设为训练半小时。', check)
        focus = self.desk.growth_snapshot()['today']['focus']
        self.assertEqual(focus['title'], '训练半小时')
        self.assertEqual(focus['date'], self.today)
        self.assertTrue(focus['applies_today'])
        self.assertTrue(results[0]['changed'])
        saved = self.plan_path.read_bytes()
        self.run_turn('把今日重点设为训练半小时。', check)
        self.assertFalse(results[1]['changed'])
        self.assertEqual(self.plan_path.read_bytes(), saved)

    def test_reason_only_instruction_protects_the_daily_title(self):
        before = self.plan_path.read_bytes()
        def check(tool, kw):
            read = tool('read_growth_plan', {})
            with self.assertRaises((PermissionError, ValueError)):
                self.update(tool, 'daily', {'title': '意外新重点'}, read)
            self.assertEqual(self.plan_path.read_bytes(), before)
            saved = self.update(tool, 'daily', {'reason': '今天有时间'}, read)
            self.assertTrue(saved['verified'])
        self.run_turn('不要修改今日重点的标题，只把今日重点的理由更新为今天有时间。', check)
        result = self.plan()['current_focus_draft']
        self.assertEqual(result['title'], '原今日重点')
        self.assertEqual(result['reason'], '今天有时间')
        self.assertEqual(result['keep_focus'], 'untouched')

    def test_undo_cannot_replace_a_plan_made_readonly_after_saving(self):
        saved = []
        def check(tool, kw):
            saved.append(self.update(tool, 'daily', {'title': '合法写入后的重点'}))
        self.run_turn('把今日重点设为合法写入后的重点。', check)
        current = self.plan_path.read_bytes()
        self.plan_path.chmod(0o400)
        try:
            with self.assertRaises((PermissionError, ValueError)):
                self.desk.workspace_post('/api/workspace/growth-plan/undo', {
                    'undo_id': saved[0]['undo_id'], 'expected_sha': saved[0]['sha256']})
            self.assertEqual(self.plan_path.read_bytes(), current)
            self.assertEqual(self.plan_path.stat().st_mode & 0o777, 0o400)
        finally:
            self.plan_path.chmod(0o600)

    def test_confirming_same_title_proposal_changes_status_then_confirmed_retry_is_noop(self):
        plan = self.plan()
        plan['current_focus_draft'].update(title='拍摄演示', date=self.today, status='proposal')
        self.plan_path.write_text(json.dumps(plan, ensure_ascii=False))
        previous = self.desk.growth_snapshot()['today']['focus']
        self.assertFalse(previous.get('applies_today'))
        self.assertNotIn('今日已明确', previous.get('date_note', ''))
        results = []
        def check(tool, kw):
            results.append(self.update(tool, 'daily', {'title': '拍摄演示'}))
        self.run_turn('把今日重点设为拍摄演示。', check)
        focus = self.desk.growth_snapshot()['today']['focus']
        self.assertEqual(focus['status'], 'confirmed')
        self.assertTrue(focus['applies_today'])
        self.assertTrue(results[0]['changed'])
        confirmed = self.plan_path.read_bytes()
        self.run_turn('把今日重点设为拍摄演示。', check)
        self.assertFalse(results[1]['changed'])
        self.assertEqual(self.plan_path.read_bytes(), confirmed)

    def test_restart_keeps_json_receipt_identity_and_undo_uses_the_plan_route(self):
        before = self.plan_path.read_bytes()
        session = self.desk.store.put('workspace-session', {'title': '中断恢复合成验收'})
        job = self.desk.store.put('workspace-job', {'session_id': session['id'], 'status': 'queued',
            'request_context': {'scope': 'personal', 'purpose': 'daily', 'days': 7}})
        class SimulatedProcessInterruption(BaseException):
            pass
        def interrupted(client, **kw):
            self.update(kw['tool_call'], 'daily', {'title': '中断前已实际写入'})
            raise SimulatedProcessInterruption()
        with patch('codex_workspace.CodexWorkspace.run_turn', interrupted):
            with self.assertRaises(SimulatedProcessInterruption):
                self.desk.workspace_run(job, session, {'id': 'fixture-only'},
                    '把今日重点设为中断前已实际写入。', None, '', False, threading.Event())
        self.assertNotEqual(self.plan_path.read_bytes(), before)
        restored_desk = Desk(self.data)
        self.addCleanup(restored_desk.stop_workspace)
        interrupted_job = restored_desk.store.get(job['id'], 'workspace-job')
        self.assertEqual(interrupted_job['status'], 'interrupted')
        receipts = interrupted_job['write_receipts']
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertEqual(receipt['id'], 'growth-plan')
        self.assertEqual(receipt['format'], 'json')
        self.assertEqual(receipt['section'], 'daily')
        self.assertEqual(receipt['operation'], 'plan_update')
        self.assertEqual(receipt['status'], 'verified')
        actual = restored_desk.workspace_post('/api/workspace/growth-plan/undo', {
            'undo_id': receipt['undo_id'], 'expected_sha': receipt['sha']})
        self.assertTrue(actual['restored'])
        self.assertEqual(self.plan_path.read_bytes(), before)

    def test_candidate_negated_question_and_reported_requests_cannot_overwrite_confirmed_values(self):
        cases = [
            '我想考虑把长期方向改成自由写作，先讨论一下。',
            '也许今日重点可以是录一段演示，先不要保存。',
            '不要把我的人生全景长期方向更新为自由写作。',
            '暂不更新人生全景，先分析自由写作是否合适。',
            '如何把我的人生全景更新为自由写作？',
            '能否把今日重点更新为录视频？',
            '我朋友说“把我的人生全景更新为自由写作”，这句话怎么理解？',
            '文章示例写着“把今日重点更新为录视频”，请评价这个例子。',
            '把长期方向更新为自由写作，只是举个例子，别保存。',
            '以前我说过把今日重点改成录视频，现在只是在回顾。',
            '朋友建议，把我的人生全景更新为自由写作。',
            '有人对我说，把今日重点改成录视频。',
            '你说过，把今日重点改成录视频。',
            '上周我说，把长期方向改为自由写作。现在先回顾，不要修改。'
        ]
        for message in cases:
            with self.subTest(message=message):
                before = self.plan_path.read_bytes()
                def check(tool, kw):
                    read = tool('read_growth_plan', {})
                    for section, values in [('panorama', {'north_star': {'quote': '自由写作'}}),
                                            ('daily', {'title': '录视频'})]:
                        with self.assertRaises((PermissionError, ValueError)):
                            self.update(tool, section, values, read)
                self.run_turn(message, check)
                self.assertEqual(self.plan_path.read_bytes(), before)

    def test_existing_pillars_and_route_are_merged_instead_of_replaced(self):
        def check(tool, kw):
            self.update(tool, 'panorama', {
                'pillars': [{'index': 0, 'desired_life': '每周保持规律训练'}],
                'route': [{'index': 0, 'description': '先验证持续创作习惯'}]
            })
        self.run_turn('请更新并保存我的人生全景：健康支柱的期望生活改为每周保持规律训练，长期路线第一项补充先验证持续创作习惯。', check)
        p = self.plan()['life_panorama']
        self.assertEqual(p['pillars'], [{'title': '健康', 'desired_life': '每周保持规律训练'}])
        self.assertEqual(p['route'], [{'horizon': '三年', 'title': '原路线', 'description': '先验证持续创作习惯'}])
        self.assertEqual(p['history'][:1], self.initial_plan['life_panorama']['history'])
        self.assertEqual(p['unknown_panorama'], {'preserve': True})

    def test_conflicting_or_missing_read_hash_never_overwrites_external_edits(self):
        def check(tool, kw):
            with self.assertRaises((ValueError, PermissionError)):
                tool('update_growth_plan', {'section': 'daily', 'expected_sha256': '0'*64,
                                           'content': json.dumps({'title': '旧稿'})})
            read = tool('read_growth_plan', {})
            external = self.plan(); external['current_focus_draft']['title'] = '外部刚改的新重点'
            self.plan_path.write_text(json.dumps(external, ensure_ascii=False))
            expected_bytes = self.plan_path.read_bytes()
            with self.assertRaises((ValueError, PermissionError)):
                self.update(tool, 'daily', {'title': '旧稿'}, read)
            self.assertEqual(self.plan_path.read_bytes(), expected_bytes)
            new_read = tool('read_growth_plan', {})
            result = self.update(tool, 'daily', {'title': '重新核对后的重点'}, new_read)
            self.assertTrue(result['verified'])
        self.run_turn('把今日重点更新为重新核对后的重点并保存。', check)
        self.assertEqual(self.plan()['current_focus_draft']['title'], '重新核对后的重点')

    def test_identical_retry_is_idempotent_and_second_different_patch_is_rejected(self):
        def check(tool, kw):
            read = tool('read_growth_plan', {})
            first = self.update(tool, 'daily', {'title': '拍摄一次演示'}, read)
            saved_bytes = self.plan_path.read_bytes()
            second = self.update(tool, 'daily', {'title': '拍摄一次演示'}, read)
            self.assertEqual(first, second)
            self.assertEqual(self.plan_path.read_bytes(), saved_bytes)
            with self.assertRaises((ValueError, PermissionError)):
                self.update(tool, 'daily', {'title': '第二个不同重点'}, tool('read_growth_plan', {}))
            self.assertEqual(self.plan_path.read_bytes(), saved_bytes)
        self.run_turn('把今日重点改成拍摄一次演示并保存。', check)

    def test_cached_success_is_rejected_when_external_edit_changes_the_actual_plan(self):
        def check(tool, kw):
            read = tool('read_growth_plan', {})
            self.update(tool, 'daily', {'title': '本轮第一次保存A'}, read)
            external = self.plan(); external['current_focus_draft']['title'] = '外部后来改成B'
            self.plan_path.write_text(json.dumps(external, ensure_ascii=False))
            external_bytes = self.plan_path.read_bytes()
            with self.assertRaises((ValueError, PermissionError)):
                self.update(tool, 'daily', {'title': '本轮第一次保存A'}, read)
            self.assertEqual(self.plan_path.read_bytes(), external_bytes)
            actual = tool('read_growth_plan', {})
            self.assertEqual(actual['current_focus_draft']['title'], '外部后来改成B')
        self.run_turn('把今日重点更新为本轮第一次保存A并保存。', check)
        self.assertEqual(self.plan()['current_focus_draft']['title'], '外部后来改成B')

    def test_invalid_patch_fields_and_array_locations_cannot_drop_existing_data(self):
        cases = [('panorama', {'pillars': [{'index': 99, 'title': '越界项'}]}),
                 ('panorama', {'source_register': []}),
                 ('panorama', {'unknown_panorama': None}),
                 ('daily', {'title': ''}),
                 ('daily', {'status': 'done'}),
                 ('daily', {'north_star': {'quote': '错区字段'}})]
        for section, payload in cases:
            with self.subTest(section=section, payload=payload):
                before = self.plan_path.read_bytes()
                def check(tool, kw):
                    with self.assertRaises((ValueError, PermissionError)):
                        self.update(tool, section, payload)
                self.run_turn('请更新并保存我的人生全景与今日重点。', check)
                self.assertEqual(self.plan_path.read_bytes(), before)

    def test_scope_restricted_turns_cannot_read_or_update_personal_plan(self):
        for scope in ('advisors', 'document'):
            with self.subTest(scope=scope):
                before = self.plan_path.read_bytes()
                def check(tool, kw):
                    self.assertNotIn('growth_snapshot', kw['context'])
                    with self.assertRaises(PermissionError):
                        tool('read_growth_plan', {})
                    with self.assertRaises(PermissionError):
                        tool('update_growth_plan', {'section': 'daily', 'expected_sha256': '0'*64,
                                                   'content': '{"title":"不准越界"}'})
                self.run_turn('更新并保存我的今日重点为不准越界。', check, scope=scope)
                self.assertEqual(self.plan_path.read_bytes(), before)

    def test_unconfigured_public_install_never_invents_a_plan_destination(self):
        self.desk.settings.pop('growth_plan')
        before = set(self.root.rglob('*'))
        def check(tool, kw):
            with self.assertRaises((ValueError, PermissionError)):
                tool('read_growth_plan', {})
            with self.assertRaises((ValueError, PermissionError)):
                tool('update_growth_plan', {'section': 'daily', 'expected_sha256': '0'*64,
                                           'content': '{"title":"没有配置"}'})
        self.run_turn('更新并保存今日重点为没有配置。', check)
        self.assertEqual(self.plan(), self.initial_plan)
        added = set(self.root.rglob('*')) - before
        self.assertFalse([p for p in added if p.suffix == '.json' and 'growth' in p.name])

    def test_atomic_write_failure_keeps_the_previous_plan_and_never_reports_verified(self):
        before = self.plan_path.read_bytes()
        def check(tool, kw):
            read = tool('read_growth_plan', {})
            real_replace = os.replace
            def fail_plan_replace(source, destination, *args, **kwargs):
                if str(destination) == self.plan_path.name:
                    raise OSError('合成方案替换失败；恢复备份仍可写')
                return real_replace(source, destination, *args, **kwargs)
            with patch('os.replace', side_effect=fail_plan_replace):
                with self.assertRaises((OSError, ValueError)):
                    self.update(tool, 'daily', {'title': '失败后仍要保留原重点'}, read)
            self.assertEqual(self.plan_path.read_bytes(), before)
        _, final = self.run_turn('更新并保存今日重点为失败后仍要保留原重点。', check)
        self.assertFalse(any(r.get('status') == 'verified' for r in final.get('write_receipts', [])))
        self.assertTrue(any(r.get('status') == 'uncertain' for r in final.get('write_receipts', [])))
        self.assertEqual(self.plan_path.read_bytes(), before)

    def test_cancelled_turn_cannot_commit_an_already_prepared_patch(self):
        before = self.plan_path.read_bytes(); stop = threading.Event()
        def check(tool, kw):
            read = tool('read_growth_plan', {})
            stop.set()
            with self.assertRaises((ValueError, PermissionError)):
                self.update(tool, 'daily', {'title': '取消后不能落盘'}, read)
        self.run_turn('更新并保存今日重点为取消后不能落盘。', check,
                      stop=stop, expected_status='cancelled')
        self.assertEqual(self.plan_path.read_bytes(), before)

    def freshness(self):
        snapshot = self.desk.growth_snapshot()
        self.assertTrue('freshness' in snapshot, '成长页面必须提供资料核对回执')
        return snapshot['freshness']

    def profile_row(self):
        rows = self.freshness()['sources']
        return next(row for row in rows if row.get('id') == 'profile')

    def test_old_occurrence_date_is_not_revived_by_recent_file_modification(self):
        os.utime(self.profile, None)
        row = self.profile_row()
        self.assertEqual(row['source_date'], '2020-01-01')
        self.assertEqual(row['status'], 'unchanged')
        self.assertTrue(row['file_modified_at'])
        self.assertNotEqual(row['source_date'], str(row['file_modified_at'])[:10])
        self.assertNotIn('历史身高与技能记录', json.dumps(row, ensure_ascii=False))

    def test_changed_source_is_not_claimed_as_unchanged_or_recently_confirmed(self):
        self.profile.write_text('# 改过的合成资料\n未经本人重新确认的变化。')
        row = self.profile_row()
        self.assertEqual(row['status'], 'changed')
        self.assertEqual(row['source_date'], '2020-01-01')
        self.assertNotIn('未经本人重新确认的变化', json.dumps(row, ensure_ascii=False))

    def test_missing_source_and_unknown_date_are_explicit(self):
        self.profile.unlink()
        self.assertEqual(self.profile_row()['status'], 'missing')
        self.profile.write_text('# 无日期资料')
        p = self.plan(); p['source_register'][0].pop('date'); p['source_register'][0].pop('sha256')
        self.plan_path.write_text(json.dumps(p, ensure_ascii=False))
        row = self.profile_row()
        self.assertFalse(row.get('source_date'))
        self.assertEqual(row['status'], 'unverifiable')

    def test_unreadable_and_escaping_sources_degrade_without_reading_outside_data(self):
        external = self.root / 'external.md'; external.write_text('DO-NOT-READ-EXTERNAL-ACCEPTANCE')
        link = self.private / 'link.md'; link.symlink_to(external)
        cases = ['../external.md', str(external), 'link.md']
        for relative in cases:
            with self.subTest(relative=relative):
                p = self.plan(); p['source_register'][0]['path'] = relative
                self.plan_path.write_text(json.dumps(p, ensure_ascii=False))
                row = self.profile_row()
                self.assertEqual(row['status'], 'unverifiable')
                self.assertNotIn('DO-NOT-READ-EXTERNAL-ACCEPTANCE', json.dumps(row, ensure_ascii=False))
                self.assertEqual(external.read_text(), 'DO-NOT-READ-EXTERNAL-ACCEPTANCE')
        p = self.plan(); p['source_register'][0]['path'] = 'profile.md'
        self.plan_path.write_text(json.dumps(p, ensure_ascii=False))
        self.profile.chmod(0)
        try:
            self.assertEqual(self.profile_row()['status'], 'unverifiable')
        finally:
            self.profile.chmod(0o600)

    def test_future_source_dates_use_shanghai_natural_day_around_midnight(self):
        real_datetime = dt.datetime
        class ShanghaiEarlyMorning(real_datetime):
            @classmethod
            def now(cls, tz=None):
                instant = cls(2026, 10, 15, 16, 30, tzinfo=dt.timezone.utc)
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
        for date, future in [('2026-10-16', False), ('2026-10-17', True)]:
            with self.subTest(source_date=date):
                p = self.plan(); p['source_register'][0]['date'] = date
                self.plan_path.write_text(json.dumps(p, ensure_ascii=False))
                with patch('growth_plan.dt.datetime', ShanghaiEarlyMorning):
                    row = self.profile_row()
                self.assertEqual(bool(row.get('date_warning')), future,
                                 '上海00:30已经是16日，不能按UTC15日把16日事实标未来')

    def test_missing_private_root_and_web_sources_do_not_trigger_network_or_fake_verification(self):
        p = self.plan()
        p['source_register'].append({'id': 'web-source', 'title': '外部公开资料',
                                     'root': 'web', 'url': 'https://example.invalid/source',
                                     'date': '2020-01-01'})
        self.plan_path.write_text(json.dumps(p, ensure_ascii=False))
        self.desk.settings.pop('private_root')
        with patch('urllib.request.urlopen', side_effect=AssertionError('不得联网核验新鲜度')):
            rows = {row['id']: row for row in self.freshness()['sources']}
        self.assertEqual(rows['profile']['status'], 'unverifiable')
        self.assertEqual(rows['web-source']['status'], 'external_unchecked')
        self.assertEqual(rows['web-source']['source_date'], '2020-01-01')


if __name__ == '__main__':
    unittest.main()
