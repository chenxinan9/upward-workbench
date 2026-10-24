"""DeepSeek credentials stay in the OS store; per-turn CLI receives only a relay token."""
from __future__ import annotations
from contextlib import contextmanager
import ctypes
import ctypes.util
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import threading

SERVICE = 'local.ruigu.deepseek'
ENV_KEY = 'RUIGU_DEEPSEEK_API_KEY'
RELAY_ENV = 'RUIGU_PROVIDER_RELAY_TOKEN'
_ENV_REVISION = secrets.token_hex(16)
MODELS = [
    {'id':'deepseek:deepseek-flash','api_model':'deepseek-flash','label':'DeepSeek V4.1 Flash','input_modalities':['text','image'],'web_search':False,
     'notice':'API 单独计费；支持图片，不提供内置联网搜索。'},
    {'id':'deepseek:deepseek-v4-pro','api_model':'deepseek-v4-pro','label':'DeepSeek V4 Pro','input_modalities':['text'],'web_search':True,
     'notice':'API 单独计费；内置联网搜索另计费用，不支持图片。'},
]


def child_environment():
    environment=dict(os.environ)
    for key in (ENV_KEY,RELAY_ENV):environment.pop(key,None)
    return environment


def _valid_key(value):
    if not isinstance(value,str) or not re.fullmatch(r'sk-[A-Za-z0-9_-]{12,253}',value):
        raise ValueError('DeepSeek API 密钥格式无效；请填写开放平台密钥，不是网页版密码')
    return value


class Keychain:
    def __init__(self, root):
        self.account = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()
        self.cf = ctypes.CDLL(ctypes.util.find_library('CoreFoundation'))
        self.sec = ctypes.CDLL(ctypes.util.find_library('Security'))
        self.cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32]
        self.cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.cf.CFDataCreate.argtypes = [ctypes.c_void_p,ctypes.c_void_p,ctypes.c_long]
        self.cf.CFDataCreate.restype = ctypes.c_void_p
        self.cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_long,ctypes.c_void_p,ctypes.c_void_p]
        self.cf.CFDictionaryCreate.restype = ctypes.c_void_p
        self.cf.CFDataGetLength.argtypes = [ctypes.c_void_p]; self.cf.CFDataGetLength.restype = ctypes.c_long
        self.cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]; self.cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        for name,args in [('SecItemCopyMatching',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]),
                          ('SecItemAdd',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]),
                          ('SecItemUpdate',[ctypes.c_void_p,ctypes.c_void_p]),('SecItemDelete',[ctypes.c_void_p])]:
            function=getattr(self.sec,name); function.argtypes=args; function.restype=ctypes.c_int32

    def _constant(self, name):
        return ctypes.c_void_p.in_dll(self.sec,name).value

    @contextmanager
    def _dict(self, pairs):
        owned=[]; keys=[]; values=[]
        try:
            for key,value in pairs.items():
                keys.append(self._constant(key))
                if isinstance(value,tuple): result=self._constant(value[0])
                elif isinstance(value,bytes):
                    result=self.cf.CFDataCreate(None,value,len(value)); owned.append(result)
                elif isinstance(value,bool): result=ctypes.c_void_p.in_dll(self.cf,'kCFBooleanTrue' if value else 'kCFBooleanFalse').value
                else:
                    result=self.cf.CFStringCreateWithCString(None,value.encode(),0x08000100); owned.append(result)
                if not result: raise ValueError('系统钥匙串暂不可用')
                values.append(result)
            key_array=(ctypes.c_void_p*len(keys))(*keys); value_array=(ctypes.c_void_p*len(values))(*values)
            dictionary=self.cf.CFDictionaryCreate(None,key_array,value_array,len(keys),None,None)
            if not dictionary: raise ValueError('系统钥匙串暂不可用')
            owned.append(dictionary); yield dictionary
        finally:
            for item in reversed(owned):self.cf.CFRelease(item)

    def _base(self):
        return {'kSecClass':('kSecClassGenericPassword',),'kSecAttrService':SERVICE,'kSecAttrAccount':self.account}

    def has_key(self):
        with self._dict(dict(self._base(),kSecUseAuthenticationUI=('kSecUseAuthenticationUIFail',))) as query:
            status=self.sec.SecItemCopyMatching(query,None)
        if status == -25300:return False
        if status != 0:raise ValueError('DeepSeek 钥匙串暂不可读；请在本机解锁后重试')
        return True

    def read(self):
        result=ctypes.c_void_p()
        with self._dict(dict(self._base(),kSecReturnData=True,kSecMatchLimit=('kSecMatchLimitOne',))) as query:
            status=self.sec.SecItemCopyMatching(query,ctypes.byref(result))
        if status == -25300:raise ValueError('尚未接入 DeepSeek API，请先在模型与连接中保存密钥')
        if status != 0:raise ValueError('DeepSeek 钥匙串读取未获允许，请在本机解锁后重试')
        try:
            length=self.cf.CFDataGetLength(result)
            if not 15 <= length <= 256:raise ValueError('DeepSeek 钥匙串记录格式无效，请重新保存')
            return _valid_key(ctypes.string_at(self.cf.CFDataGetBytePtr(result),length).decode())
        finally:
            if result:self.cf.CFRelease(result)

    def save(self, key):
        value=_valid_key(key).encode()
        with self._dict(self._base()) as query, self._dict({'kSecValueData':value}) as data:
            status=self.sec.SecItemUpdate(query,data)
        if status == -25300:
            with self._dict(dict(self._base(),kSecValueData=value)) as query:status=self.sec.SecItemAdd(query,None)
        if status != 0:raise ValueError('DeepSeek 密钥未保存到系统钥匙串；请检查本机授权后重试')

    def remove(self):
        with self._dict(self._base()) as query:status=self.sec.SecItemDelete(query)
        if status not in (0,-25300):raise ValueError('DeepSeek 钥匙串记录未删除，请在本机检查授权')


def credential_state(root):
    storage='macos-keychain' if sys.platform=='darwin' else 'environment'
    try:
        configured=Keychain(root).has_key() if sys.platform=='darwin' else bool(_valid_key(os.environ[ENV_KEY]) if os.environ.get(ENV_KEY) else False)
        error=''
    except (ValueError,OSError,KeyError):configured=False; error='系统凭据暂不可读，请检查本机授权或重新接入'
    return {'id':'deepseek','label':'DeepSeek API','configured':configured,'storage':storage,
            'can_save':sys.platform=='darwin','error':error,
            'notice':'使用本人的 DeepSeek 开放平台 API，按该账户单独计费；App/网页版会员不能代替 API 密钥。'}


def save_deepseek_key(root, key):
    if sys.platform!='darwin':raise ValueError('此系统请在启动睿顾台前设置 RUIGU_DEEPSEEK_API_KEY，界面不保存明文密钥')
    _valid_key(key)
    _record_revision(root)
    Keychain(root).save(key)
    return credential_state(root)


def remove_deepseek_key(root):
    if sys.platform!='darwin':raise ValueError('此系统请移除启动环境中的 RUIGU_DEEPSEEK_API_KEY 后重启工作台')
    _record_revision(root)
    Keychain(root).remove()
    return credential_state(root)


def _record_revision(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=root/'provider-revision.json'
    if target.is_symlink():raise ValueError('服务配置元数据不可为符号链接')
    temporary=root/('provider-'+secrets.token_hex(8)+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump({'deepseek':secrets.token_hex(16)},stream);stream.flush();os.fsync(stream.fileno())
        temporary.replace(target)
        directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:temporary.unlink(missing_ok=True)


def provider_revision(root):
    if sys.platform!='darwin':return _ENV_REVISION
    try:
        value=json.loads((Path(root)/'provider-revision.json').read_text()).get('deepseek','')
        return value if isinstance(value,str) and re.fullmatch(r'[a-f0-9]{32}',value) else ''
    except (OSError,ValueError,AttributeError):return ''


def _credential(root):
    if sys.platform=='darwin':return Keychain(root).read()
    return _valid_key(os.environ.get(ENV_KEY,''))


class DeepSeekRelay:
    """One job, one token, one fixed upstream. No credentials in the CLI process."""
    def __init__(self, key, cancel_event, model, *, connection_factory=None):
        if model not in {row['api_model'] for row in MODELS}:raise ValueError('DeepSeek 模型无效')
        self.model=model
        self.key=_valid_key(key); self.cancel=cancel_event; self.stop=threading.Event()
        self.token=secrets.token_urlsafe(32); self.connections=set(); self.sockets=set(); self.lock=threading.Lock()
        self.connection_factory=connection_factory or http.client.HTTPSConnection
        relay=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.send_error(405)
            def do_POST(self):
                host='127.0.0.1:'+str(self.server.server_port)
                if (self.headers.get('Host')!=host or len(self.headers.get_all('Host',[]))!=1 or self.path!='/responses' or self.headers.get('Origin') is not None or
                        len(self.headers.get_all('Authorization',[]))!=1 or
                        not hmac.compare_digest(self.headers.get('Authorization','').encode(),('Bearer '+relay.token).encode()) or
                        relay.cancel.is_set() or relay.stop.is_set()):
                    self.send_error(403);return
                try:length=int(self.headers.get('Content-Length',''))
                except ValueError:length=0
                if (not 0 < length <= 128*1024*1024 or len(self.headers.get_all('Content-Length',[]))!=1 or
                        self.headers.get('Transfer-Encoding') is not None or self.headers.get('Content-Type','').split(';')[0]!='application/json'):
                    self.send_error(413);return
                self.connection.settimeout(30)
                try:body=self.rfile.read(length)
                except (OSError,TimeoutError):return
                if len(body)!=length or relay.cancel.is_set() or relay.stop.is_set():return
                try:payload=json.loads(body)
                except (ValueError,UnicodeError):self.send_error(400);return
                if not isinstance(payload,dict) or payload.get('model')!=relay.model:
                    self.send_error(400);return
                connection=relay.connection_factory('api.deepseek.com',timeout=120)
                with relay.lock:relay.connections.add(connection)
                try:
                    if relay.cancel.is_set() or relay.stop.is_set():return
                    connection.request('POST','/responses',body=body,headers={
                        'Authorization':'Bearer '+relay.key,'Content-Type':'application/json','Accept':'text/event-stream'})
                    upstream_socket=connection.sock
                    with relay.lock:
                        if upstream_socket:relay.sockets.add(upstream_socket)
                    if relay.cancel.is_set() or relay.stop.is_set():return
                    response=connection.getresponse()
                    if not 200 <= response.status < 300:
                        code=response.status if response.status in (400,401,402,403,413,429,500,502,503,504) else 502
                        data=json.dumps({'error':{'message':'DeepSeek API request failed','type':'provider_error','code':code}}).encode()
                        self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
                    self.send_response(response.status);self.send_header('Content-Type','text/event-stream');self.send_header('Connection','close');self.end_headers()
                    while not relay.cancel.is_set() and not relay.stop.is_set():
                        block=response.read1(65536)
                        if not block or relay.cancel.is_set() or relay.stop.is_set():break
                        self.wfile.write(block);self.wfile.flush()
                except (OSError,TimeoutError,http.client.HTTPException):
                    self.close_connection=True
                finally:
                    connection.close()
                    with relay.lock:
                        relay.connections.discard(connection)
                        if 'upstream_socket' in locals():relay.sockets.discard(upstream_socket)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True

    def __enter__(self):
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.05},daemon=True);self.thread.start()
        self.watcher=threading.Thread(target=self._watch,daemon=True);self.watcher.start()
        return self

    @property
    def url(self):return 'http://127.0.0.1:'+str(self.server.server_port)

    def _watch(self):
        while not self.stop.wait(.05):
            if self.cancel.is_set():self._close_connections();return

    def _close_connections(self):
        with self.lock:connections=list(self.connections);sockets=list(self.sockets)
        for sock in sockets:
            try:sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        for connection in connections:
            try:
                if connection.sock:connection.sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError,OSError):pass
            connection.close()

    def __exit__(self,*args):
        self.stop.set();self._close_connections();self.server.shutdown();self.server.server_close()
        self.thread.join(timeout=1);self.watcher.join(timeout=1)
        self.key='';self.token=''


def _model_rows():
    rows=[]
    for model in MODELS:
        rows.append({'slug':model['api_model'],'display_name':model['label'],'description':'DeepSeek API model',
            'visibility':'list','base_instructions':'','prefer_websockets':False,'support_verbosity':True,'default_verbosity':'low',
            'apply_patch_tool_type':'freeform','web_search_tool_type':'text','input_modalities':model['input_modalities'],
            'supports_image_detail_original':'image' in model['input_modalities'],
            'truncation_policy':{'mode':'tokens','limit':10000},'supports_parallel_tool_calls':True,
            'context_window':1048576,'effective_context_window_percent':95,'default_reasoning_level':'high',
            'supported_reasoning_levels':[{'effort':effort,'description':effort} for effort in ('low','high','max')],
            'shell_type':'shell_command','supported_in_api':True,'priority':len(rows)+1,'supports_reasoning_summaries':False,
            'experimental_supported_tools':[],'supports_search_tool':model['web_search'],'default_service_tier':None})
    return rows


@contextmanager
def deepseek_runtime(root, model, cancel_event):
    if model not in {row['api_model'] for row in MODELS}:raise ValueError('DeepSeek 模型无效，未发送')
    root=Path(root).resolve();home=root/'provider-runtime'/'deepseek'
    for path in (root/'provider-runtime',home):
        if path.is_symlink():raise ValueError('DeepSeek 运行目录不可为符号链接')
        path.mkdir(mode=0o700,exist_ok=True);path.chmod(0o700)
    catalog=home/'models.json'
    if catalog.is_symlink():raise ValueError('DeepSeek 模型目录不可为符号链接')
    expected=json.dumps({'models':_model_rows()},ensure_ascii=False)
    if not catalog.exists() or catalog.read_text()!=expected:
        temporary=home/('models-'+secrets.token_hex(8)+'.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'w') as stream:stream.write(expected)
            temporary.replace(catalog)
        finally:temporary.unlink(missing_ok=True)
    with DeepSeekRelay(_credential(root),cancel_event,model) as relay:
        environment={key:value for key,value in os.environ.items() if key in ('PATH','HOME','TMPDIR','TEMP','TMP','LANG','LC_ALL','LC_CTYPE','SYSTEMROOT','WINDIR')}
        environment.update(CODEX_HOME=str(home),**{RELAY_ENV:relay.token})
        flags={'model_provider':'ruigu_deepseek','model_catalog_json':str(catalog),'forced_login_method':'api',
            'show_raw_agent_reasoning':False,'features.remote_models':False,
            'model_providers.ruigu_deepseek.name':'DeepSeek API','model_providers.ruigu_deepseek.base_url':relay.url,
            'model_providers.ruigu_deepseek.wire_api':'responses','model_providers.ruigu_deepseek.env_key':RELAY_ENV,
            'model_providers.ruigu_deepseek.requires_openai_auth':False,'model_providers.ruigu_deepseek.request_max_retries':0,
            'model_providers.ruigu_deepseek.stream_max_retries':0,'shell_environment_policy.inherit':'none'}
        for key,value in environment.items():
            if key not in ('CODEX_HOME',RELAY_ENV):flags['shell_environment_policy.set.'+key]=value
        if model!='deepseek-v4-pro':flags['web_search']='disabled'
        yield {'home':home,'env':environment,'flags':flags}
