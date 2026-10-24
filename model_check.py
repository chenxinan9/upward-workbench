"""User-triggered connection check using a fixed public prompt, never a draft."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading


class ModelCheckMixin:
    def model_check_status(self):
        with self.runlock:
            return json.loads(json.dumps(getattr(self,'_model_check',{'status':'idle','results':[]})))

    def check_models(self, ident=None):
        with self.runlock:
            state=getattr(self,'_model_check',{})
            if state.get('status')=='running' or (getattr(self,'_model_check_thread',None) and self._model_check_thread.is_alive()): return self.model_check_status()
            catalog=self.model_catalog()
            ids=[self.resolve_model(ident,require_verified=False)['id']] if ident else [m['id'] for m in catalog['models'] if
                 (m.get('selectable') and m['id']==catalog['default_id']) or
                 (m.get('availability') in ('verified_route','unverified_route') and
                  (m['id'].startswith('gemini-web/') or m.get('is_default')))][:4]
            if not ids: raise ValueError('本机尚未发现可检查的模型连接')
            if state.get('finished_at') and state.get('models')==ids:
                elapsed=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(state['finished_at'])).total_seconds()
                if elapsed<30:return self.model_check_status()
            self._model_check_stop=threading.Event()
            self._model_check={'status':'running','models':ids,'results':[],'started_at':dt.datetime.now(dt.timezone.utc).isoformat()}
            self._model_check_thread=threading.Thread(target=self._check_worker,args=(ids,),daemon=True)
            self._model_check_thread.start()
            return self.model_check_status()

    def _probe_model(self, ident):
        chosen=self.resolve_model(ident,require_verified=False)
        if chosen.get('service_id')=='deepseek':
            from codex_workspace import CodexWorkspace
            with tempfile.TemporaryDirectory(prefix='ruigu-deepseek-check-') as tmp:
                result=CodexWorkspace(self.cli,tmp,provider=chosen,data_root=self.store.path).run_turn(
                    '请只回答 OK。这是公开连接检查，不读取文件，不执行工具。',chosen['api_model'],
                    cancel_event=self._model_check_stop,context={'scope':'advisors'},timeout=100)
            if result.get('status')!='completed' or result.get('actual_model')!=chosen['api_model']:
                raise ValueError('未收到与目标模型一致的成功回复')
            return result['actual_model']
        with tempfile.TemporaryDirectory(prefix='upward-model-check-') as tmp:
            probe=self.__class__(Path(tmp)/'data');probe.cli=self.cli
            job=probe.store.put('job',{'status':'queued','effective_model':ident,'organize':False})
            folder=Path(tmp)/'run';folder.mkdir()
            cmd=self.isolated_model_command()+['-c','model='+json.dumps(ident)]
            if chosen.get('routing_effort'):cmd+=['-c','model_reasoning_effort='+json.dumps(chosen['routing_effort'])]
            with self.runlock:
                if self._model_check_stop.is_set():raise ValueError('检查已停止')
                self._active_model_probe=(probe,job['id'])
            try:reply=probe.stream_model_text(job,cmd,folder,'请只回答 OK。这是连接检查，不需要读取任何文件或执行工具。','answer',100)
            finally:
                with self.runlock:self._active_model_probe=None
            actual=probe.store.get(job['id']).get('actual_model')
            if not reply or actual!=ident:raise ValueError('未收到与目标模型一致的成功回复')
            return actual

    def _check_worker(self, ids):
        for ident in ids:
            if self._model_check_stop.is_set():break
            try:
                actual=self._probe_model(ident)
                if self._model_check_stop.is_set():break
                self.verify_model(ident,actual)
                result={'id':ident,'status':'verified','message':'已通过真实调用验证'}
            except Exception:
                if self._model_check_stop.is_set():break
                try:self.invalidate_model(ident)
                except (ValueError,OSError):pass
                result={'id':ident,'status':'failed','message':'本次未回答核验成功；目录提供状态与试答结果分别显示，请检查登录、额度或连接。'}
            with self.runlock:self._model_check['results'].append(result)
        with self.runlock:self._model_check.update(status='cancelled' if self._model_check_stop.is_set() else 'completed',finished_at=dt.datetime.now(dt.timezone.utc).isoformat())

    def stop_model_check(self):
        with self.runlock:
            stop=getattr(self,'_model_check_stop',None)
            if stop:stop.set()
            active=getattr(self,'_active_model_probe',None)
            if active:active[0].cancel(active[1])
            if getattr(self,'_model_check',{}).get('status')=='running':self._model_check['status']='cancelled'
            worker=getattr(self,'_model_check_thread',None)
        if worker and worker is not threading.current_thread():worker.join(timeout=3)
        return self.model_check_status()
