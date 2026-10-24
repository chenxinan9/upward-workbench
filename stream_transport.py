"""Isolated app-server transport: public summaries and answer deltas, never raw reasoning."""
import datetime as dt
import json
import os
import re
import selectors
import signal
import subprocess
import time
from provider_access import child_environment


def answer_prefix(raw, structured):
    """Decode only the first answer field; never expose partially generated capture items."""
    if not structured:
        return raw[:100000]
    match = re.match(r'^\s*(?:```(?:json)?\s*)?\{\s*"answer"\s*:\s*"', raw)
    if not match:
        return ''
    out = []; i = match.end()
    while i < len(raw):
        c = raw[i]
        if c == '"': break
        if c == '\\':
            if i + 1 >= len(raw): break
            end = i + (6 if raw[i+1] == 'u' else 2)
            if end > len(raw): break
            try: out.append(json.loads('"' + raw[i:end] + '"'))
            except ValueError: break
            i = end
        else:
            if ord(c) < 32: break
            out.append(c); i += 1
    # A UTF-16 escape pair can arrive across chunks. Keep only complete Unicode.
    return ''.join(out).encode('utf-16', 'surrogatepass').decode('utf-16', 'ignore')[:100000]


class StreamTransportMixin:
    def stream_model_text(self, job, cmd, folder, prompt, stage, timeout):
        proc = None; selector = selectors.DefaultSelector(); stderr = bytearray()
        outgoing = bytearray(); pending = bytearray(); metadata = []; items = {}; summaries = {}
        result = None; thread_id = None; turn_id = None; last_publish = 0; total = 0
        saved=self.store.get(job['id'],'job')
        feedback = {k:saved[k] for k in ('analysis_updates','actual_model') if k in saved}
        feedback.update(self._live_feedback.get(job['id'],{}))
        prior = list(feedback.get('analysis_updates', [])); completed = False
        def cancelled():
            return self.store.get(job['id'], 'job')['status'] in ('cancelled', 'cancelling')
        def send(value):
            outgoing.extend((json.dumps(value, ensure_ascii=False)+'\n').encode())
            try: selector.get_key(proc.stdin)
            except KeyError: selector.register(proc.stdin, selectors.EVENT_WRITE, 'stdin')
        def publish(force=False):
            nonlocal last_publish
            if not force and time.monotonic()-last_publish < .2: return
            last_publish = time.monotonic()
            updates = prior + [{'id':stage+':'+key, 'kind':'summary', 'text':value, 'at':stamp, 'stage':stage}
                               for key, (value, stamp) in summaries.items() if value]
            updates += [{'id':stage+':'+key, 'kind':'commentary', 'text':v['text'], 'at':v['at'], 'stage':stage}
                        for key, v in items.items() if v.get('phase') == 'commentary' and v['text']]
            feedback['analysis_updates'] = updates[-20:]
            if stage == 'answer':
                raw = ''.join(v['text'] for v in items.values() if v.get('phase') != 'commentary')
                feedback['response_preview'] = answer_prefix(raw, job.get('organize',False))
            with self.runlock:
                if not cancelled(): self._live_feedback[job['id']] = dict(feedback)
        def handle(event):
            nonlocal thread_id, turn_id, result, completed
            if not isinstance(event, dict): raise ValueError('实时事件结构异常，未记为成功')
            method = event.get('method'); params = event.get('params', {})
            if method and not isinstance(params,dict): raise ValueError('实时事件内容异常')
            if 'id' in event and method:
                # No approval, shell, file, or dynamic tool request can be fulfilled here.
                raise ValueError('本轮模型请求了未开放的工具，已停止')
            if 'id' in event:
                if 'error' in event: raise ValueError('模型接口拒绝本轮请求；未自动更换模型或重发')
                r = event.get('result', {})
                if not isinstance(r,dict): raise ValueError('模型接口结果结构异常')
                if event['id'] == 1:
                    send({'method':'initialized'})
                    send({'id':2,'method':'thread/start','params':{'cwd':str(folder),'ephemeral':True,'approvalPolicy':'never','sandbox':'read-only','developerInstructions':'','baseInstructions':''}})
                elif event['id'] == 2:
                    thread_id = r.get('thread',{}).get('id')
                    if not thread_id: raise ValueError('没有收到本轮线程标识')
                    actual = r.get('model')
                    expected = job.get('effective_model')
                    expected = expected.get('id') if isinstance(expected,dict) else expected
                    if expected and actual != expected: raise ValueError('模型回执与所选模型不一致，未发送本轮资料')
                    feedback['actual_model'] = actual
                    send({'id':3,'method':'turn/start','params':{'threadId':thread_id,'input':[{'type':'text','text':prompt}],'summary':'concise'}})
                elif event['id'] == 3: turn_id = r.get('turn',{}).get('id') or turn_id
                return
            if method == 'error':
                raise ValueError('模型接口报告错误；本轮未记为成功')
            if params.get('threadId') and params['threadId'] != thread_id: return
            if turn_id and params.get('turnId') and params['turnId'] != turn_id: return
            if method == 'turn/started':
                turn_id = params.get('turn',{}).get('id') or turn_id
                self.progress_event(job['id'],stage+'-accepted','模型已开始处理：'+('选择顾问' if stage=='routing' else '综合回答'))
            elif method in ('item/started','item/completed'):
                item = params.get('item')
                if not isinstance(item,dict): raise ValueError('模型条目结构异常')
                kind = item.get('type'); key = item.get('id')
                if kind not in ('agentMessage','reasoning','userMessage'):
                    raise ValueError('模型执行了未开放的条目，未记为成功')
                if kind == 'agentMessage':
                    if not isinstance(key,str) or not isinstance(item.get('text',''),str): raise ValueError('回复条目格式异常')
                    old = items.get(key,{})
                    items[key] = {'text':item['text'] if 'text' in item else old.get('text',''),'phase':item.get('phase') or old.get('phase'),'at':old.get('at',dt.datetime.now(dt.timezone.utc).isoformat())}
                # reasoning.content and all raw reasoning/textDelta are deliberately ignored.
            elif method == 'item/reasoning/summaryTextDelta':
                key = str(params.get('itemId',''))+':'+str(params.get('summaryIndex',0)); delta = params.get('delta')
                if not isinstance(delta,str): raise ValueError('摘要格式异常')
                old, stamp = summaries.get(key,('',dt.datetime.now(dt.timezone.utc).isoformat()))
                summaries[key] = ((old+delta)[:12000],stamp)
            elif method == 'item/agentMessage/delta':
                key = params.get('itemId'); delta = params.get('delta')
                if key not in items or not isinstance(delta,str): raise ValueError('未收到回复条目开始事件')
                items[key]['text'] = (items[key]['text']+delta)[:120000]
            elif method == 'turn/completed':
                turn = params.get('turn',{})
                if turn.get('status') != 'completed': raise ValueError('模型本轮未完成；未记为成功')
                result = ''.join(v['text'] for v in items.values() if v.get('phase') != 'commentary').strip()
                if not result: raise ValueError('本轮未返回有效回答')
                completed = True
            if len(items)>32 or len(summaries)>32: raise ValueError('本轮条目过多，已停止')
            publish()
        try:
            with self.runlock:
                if cancelled(): return None
                proc = subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,bufsize=0,env=child_environment())
                self.processes[job['id']] = proc; self.store.change(job['id'],'job',status='running')
            for pipe in (proc.stdin,proc.stdout,proc.stderr): os.set_blocking(pipe.fileno(),False)
            selector.register(proc.stdout,selectors.EVENT_READ,'stdout'); selector.register(proc.stderr,selectors.EVENT_READ,'stderr')
            send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'upward_workbench','title':'向上工作台','version':'0.3.0'},'capabilities':{'experimentalApi':True}}})
            deadline = time.monotonic()+timeout
            while not completed:
                if cancelled(): return None
                if time.monotonic() >= deadline: raise ValueError('模型实时调用超时；未自动重发')
                for key, _ in selector.select(.1):
                    if key.data == 'stdin':
                        n = os.write(proc.stdin.fileno(),outgoing[:65536]); del outgoing[:n]
                        if not outgoing: selector.unregister(proc.stdin)
                        continue
                    chunk = os.read(key.fileobj.fileno(),65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if key.data == 'stdout' and not completed: raise ValueError('实时连接提前结束，未记为成功')
                        continue
                    total += len(chunk)
                    if total > 8_000_000: raise ValueError('实时返回超出本轮大小上限')
                    if key.data == 'stderr': stderr.extend(chunk[:max(0,64000-len(stderr))]); continue
                    pending.extend(chunk)
                    while b'\n' in pending:
                        line, _, rest = pending.partition(b'\n'); pending = bytearray(rest)
                        if len(line)>1_000_000: raise ValueError('实时条目过大')
                        try: event = json.loads(line)
                        except (ValueError,UnicodeDecodeError): raise ValueError('实时返回不是有效事件')
                        metadata.append({'type':event.get('method','rpc') if isinstance(event,dict) else 'invalid','at':dt.datetime.now(dt.timezone.utc).isoformat()})
                        handle(event)
                    if len(pending)>1_000_000: raise ValueError('实时条目过大')
                publish()
            publish(True)
            return None if cancelled() else result
        finally:
            selector.close()
            if proc:
                # app-server is per-call and ephemeral; no server or pipe stays resident.
                try: os.killpg(proc.pid,signal.SIGKILL)
                except (ProcessLookupError,PermissionError):
                    if proc.poll() is None:raise
                proc.wait(timeout=3)
                for pipe in (proc.stdin,proc.stdout,proc.stderr): pipe.close()
            with self.runlock:
                if self.processes.get(job['id']) is proc: self.processes.pop(job['id'],None)
                latest = self._live_feedback.pop(job['id'],{})
                if latest and not cancelled(): self.store.change(job['id'],'job',**latest)
            # Only event names/times in the stream receipt, never raw hidden reasoning.
            (folder/(stage+'-stream-events.json')).write_text(json.dumps(metadata,ensure_ascii=False))
            (folder/(stage+'-stream-stderr.txt')).write_bytes(stderr)
