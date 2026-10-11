import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stream_transport import StreamTransportMixin, answer_prefix


class FakeStore:
    def __init__(self): self.job = {'id': 'j1', 'status': 'queued'}
    def get(self, ident, kind=None): return dict(self.job)
    def change(self, ident, kind=None, **values): self.job.update(values); return dict(self.job)


class Host(StreamTransportMixin):
    def __init__(self):
        self.store = FakeStore()
        self.runlock = threading.RLock()
        self.processes = {}
        self._live_feedback = {}
    def progress_event(self, ident, key, text):
        self.store.job.setdefault('progress_events', []).append({'key': key, 'text': text})


class StreamTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.host = Host()
        self.job = dict(self.host.store.job, effective_model='fixture/model', organize=False)

    def tearDown(self): self.tmp.cleanup()

    def server(self, tail, model='fixture/model'):
        script = self.root / 'server.py'
        script.write_text('''import sys,json,time,pathlib

def emit(method, params):
 print(json.dumps({'method':method,'params':params}),flush=True)

def reply(ident, result):
 print(json.dumps({'id':ident,'result':result}),flush=True)

line=json.loads(sys.stdin.readline());reply(1,{})
line=json.loads(sys.stdin.readline())
line=json.loads(sys.stdin.readline());reply(2,{'thread':{'id':'thread-1'},'model':''' + repr(model) + '''})
line=sys.stdin.readline()
if not line:sys.exit(0)
reply(3,{'turn':{'id':'turn-1'}})
emit('turn/started',{'threadId':'thread-1','turn':{'id':'turn-1'}})
''' + tail)
        return [sys.executable, '-u', str(script)]

    def call(self, cmd, timeout=3):
        return self.host.stream_model_text(self.job, cmd, self.root, 'PUBLIC_PROBE_PROMPT', 'answer', timeout)

    def start(self, cmd, timeout=3):
        result, errors = [], []
        def work():
            try: result.append(self.call(cmd, timeout))
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=work)
        thread.start()
        return thread, result, errors

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(.01)
        self.fail('预期流事件未及时出现')

    def test_real_incremental_summary_and_answer_before_finish_raw_reasoning_hidden(self):
        gate = self.root / 'gate'
        raw_first = '{"answer":"先做一步\\n'
        raw_final = '{"answer":"先做一步\\n然后验证","captures":[{"body":"PRIVATE_CAPTURE"}]}'
        self.job['organize'] = True
        cmd = self.server('''emit('item/started',{'threadId':'thread-1','item':{'id':'reason','type':'reasoning','content':['PRIVATE_REASONING']}})
emit('item/reasoning/textDelta',{'threadId':'thread-1','itemId':'reason','delta':'PRIVATE_RAW_DELTA'})
emit('item/reasoning/summaryTextDelta',{'threadId':'thread-1','itemId':'reason','summaryIndex':0,'delta':'根据目标确定下一步。'})
emit('item/started',{'threadId':'thread-1','item':{'id':'a1','type':'agentMessage','text':'','phase':'final_answer'}})
emit('item/agentMessage/delta',{'threadId':'thread-1','itemId':'a1','delta':''' + repr(raw_first) + '''})
while not pathlib.Path(''' + repr(str(gate)) + ''').exists():time.sleep(.01)
emit('item/completed',{'threadId':'thread-1','item':{'id':'a1','type':'agentMessage','text':''' + repr(raw_final) + ''','phase':'final_answer'}})
emit('turn/completed',{'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}})
time.sleep(10)
''')
        thread, results, errors = self.start(cmd)
        try:
            self.wait_for(lambda: bool(self.host._live_feedback.get('j1', {}).get('response_preview')))
            self.assertTrue(thread.is_alive())
            live = self.host._live_feedback['j1']
            self.assertEqual(live['response_preview'], '先做一步\n')
            self.assertEqual(live['analysis_updates'][0]['text'], '根据目标确定下一步。')
            self.assertNotIn('PRIVATE', json.dumps(live))
        finally:
            gate.touch(); thread.join(4)
        self.assertFalse(errors)
        self.assertEqual(results, [raw_final])
        self.assertEqual(self.host.store.job['response_preview'], '先做一步\n然后验证')
        self.assertEqual(self.host.processes, {})
        self.assertNotIn('PRIVATE', (self.root / 'answer-stream-events.json').read_text())
        self.assertNotIn('PRIVATE', json.dumps(self.host.store.job))

    def test_model_mismatch_rejects_before_sending_prompt(self):
        receipt = self.root / 'received'
        cmd = self.server('pathlib.Path('+repr(str(receipt))+').write_text(line)\n', model='different/model')
        with self.assertRaisesRegex(ValueError, '模型回执与所选模型不一致'): self.call(cmd)
        self.assertFalse(receipt.exists())
        self.assertEqual(self.host.processes, {})

    def test_unsolicited_tool_request_and_completed_tool_are_rejected(self):
        for tail in (
            "print(json.dumps({'id':90,'method':'item/tool/call','params':{'name':'shell'}}),flush=True)\ntime.sleep(10)\n",
            "emit('item/completed',{'threadId':'thread-1','item':{'id':'tool','type':'commandExecution','command':'whoami'}})\ntime.sleep(10)\n",
        ):
            with self.subTest(tail=tail):
                self.host = Host()
                with self.assertRaisesRegex(ValueError, '未开放'): self.call(self.server(tail))
                self.assertEqual(self.host.processes, {})

    def test_cancel_stops_process_and_cannot_publish_late_feedback(self):
        cmd = self.server("emit('item/reasoning/summaryTextDelta',{'threadId':'thread-1','itemId':'r','delta':'公开摘要'})\ntime.sleep(10)\n")
        thread, results, errors = self.start(cmd)
        self.wait_for(lambda: 'j1' in self.host._live_feedback)
        with self.host.runlock: self.host.store.change('j1', status='cancelled')
        thread.join(3)
        self.assertFalse(thread.is_alive()); self.assertFalse(errors)
        self.assertEqual(results, [None]); self.assertEqual(self.host.processes, {})
        self.assertNotIn('analysis_updates', self.host.store.job)
        self.assertEqual(self.host._live_feedback, {})

    def test_timeout_closes_server_without_success(self):
        start = time.monotonic()
        with self.assertRaisesRegex(ValueError, '超时'): self.call(self.server('time.sleep(10)\n'), .2)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(self.host.processes, {})

    def test_failed_turn_and_premature_eof_never_return_partial_as_success(self):
        for tail in ("emit('turn/completed',{'threadId':'thread-1','turn':{'status':'failed'}})\n", 'sys.exit(0)\n'):
            with self.subTest(tail=tail):
                self.host = Host()
                cmd = self.server("emit('item/started',{'threadId':'thread-1','item':{'id':'a1','type':'agentMessage','text':'partial'}})\n" + tail)
                with self.assertRaises(ValueError): self.call(cmd)
                self.assertEqual(self.host.processes, {})

    def test_other_thread_events_do_not_leak_into_current_turn(self):
        cmd = self.server("""emit('item/reasoning/summaryTextDelta',{'threadId':'other-thread','itemId':'r','delta':'OTHER_PRIVATE'})
emit('item/started',{'threadId':'thread-1','item':{'id':'a','type':'agentMessage','text':'OK'}})
emit('turn/completed',{'threadId':'thread-1','turn':{'status':'completed'}})
time.sleep(10)
""")
        self.assertEqual(self.call(cmd), 'OK')
        self.assertNotIn('OTHER_PRIVATE', json.dumps(self.host.store.job))

    def test_prefix_decoder_preserves_chunked_escapes_and_hides_captures(self):
        self.assertEqual(answer_prefix('{"answer":"A\\u4f', True), 'A')
        self.assertEqual(answer_prefix('{"answer":"A\\u4f60\\nB","captures":["SECRET"]}', True), 'A你\nB')
        self.assertEqual(answer_prefix('{"captures":["SECRET"],"answer":"A"}', True), '')
        self.assertEqual(answer_prefix('{"answer":"\\ud83d', True), '')
        self.assertEqual(answer_prefix('{"answer":"\\ud83d\\ude00"}', True), '😀')
        self.assertEqual(answer_prefix('普通答案', False), '普通答案')


if __name__ == '__main__': unittest.main()
