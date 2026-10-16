"""Explicit KB shares; connection secrets and preview bodies never enter the desk store."""
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

SHARED_ROOT = 'shared-growth'
SHARE_ID = re.compile(r'SHR-[0-9a-f]{32}')
SHA = re.compile(r'[0-9a-f]{64}')
CONNECTION = Path.home() / 'Library/Application Support/本地知识工作台/采集管理/分享权限/existing/growth/connection.json'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class SharedError(ValueError):
    def __init__(self, message, code='shared_unavailable', retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


class SharedKnowledge:
    def __init__(self, connection=CONNECTION):
        self.connection = Path(connection)
        self.previews = {}
        self.lock = threading.RLock()

    def _connection(self):
        try:
            if any(p.is_symlink() for p in (self.connection, *self.connection.parents)):
                raise ValueError()
            fd = os.open(self.connection, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096:
                    raise ValueError()
                value = json.load(handle)
            if (value.get('version') != 1 or value.get('protocol_version') != 1
                    or value.get('application') != 'local-knowledge-workbench' or value.get('target') != 'growth'
                    or value.get('online') is not True or type(value.get('port')) is not int or not 1024 <= value['port'] <= 65535
                    or not isinstance(value.get('instance_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', value['instance_id'])
                    or not isinstance(value.get('token'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', value['token'])):
                raise ValueError()
            return value
        except (OSError, ValueError, TypeError):
            raise SharedError('共享连接尚未就绪；请先在知识工作台明确分享片段。', retryable=True) from None

    def request(self, path='', data=None):
        connection = self._connection()
        headers = {'X-Knowledge-Share-Token': connection['token'], 'X-Knowledge-Share-Instance': connection['instance_id']}
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            headers['Content-Type'] = 'application/json'
        client = http.client.HTTPConnection('127.0.0.1', connection['port'], timeout=6)
        response = None
        try:
            client.request('POST' if data is not None else 'GET', '/api/v1/shared/growth' + path, body, headers)
            socket = client.sock
            deadline = time.monotonic() + 6
            response = client.getresponse()
            chunks = []
            size = 0
            while size <= 100000 and not response.isclosed():
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise OSError('shared deadline')
                socket.settimeout(remaining)
                chunk = response.read1(min(4096,100001-size))
                if not chunk: break
                chunks.append(chunk); size += len(chunk)
            raw = b''.join(chunks)
            if len(raw) > 100000 or response.status in range(300, 400):
                raise SharedError('共享服务回执无效，未使用资料。')
            value = json.loads(raw)
            if not isinstance(value,dict): raise SharedError('共享服务回执无效。')
            if response.status != 200 or value.get('ok') is False or value.get('error'):
                error = value.get('error') or {}
                raise SharedError(error.get('message', '共享来源暂不可核验，请重试。'), error.get('code', 'shared_unavailable'), error.get('retryable', False))
            value = value.get('data', value)
            if value.get('instance_id') != connection['instance_id'] or type(value.get('generation')) is not int or not SHA.fullmatch(str(value.get('scope_fingerprint', ''))):
                raise SharedError('共享实例或范围回执无效，请重新预览。', 'shared_scope_changed')
            return value
        except (OSError, http.client.HTTPException, json.JSONDecodeError):
            raise SharedError('共享来源暂不可核验；问题与片段选择保留，请重试。', retryable=True) from None
        finally:
            if response is not None: response.close()
            client.close()

    @staticmethod
    def shares(values):
        if (not isinstance(values, list) or not 1 <= len(values) <= 12
                or any(type(v) is not dict or set(v) != {'share_id', 'expected_version'} or not SHARE_ID.fullmatch(str(v['share_id']))
                       or type(v['expected_version']) is not int or v['expected_version'] < 1 for v in values)
                or len({v['share_id'] for v in values}) != len(values)):
            raise SharedError('请选择 1 至 12 个不同的已共享片段。', 'shared_invalid_selection')
        return copy.deepcopy(values)

    @staticmethod
    def scope(value):
        return {key: value[key] for key in ('instance_id', 'generation', 'scope_fingerprint')}

    def _payload(self, shares, scope):
        if (type(scope) is not dict or set(scope)!={'instance_id','generation','scope_fingerprint'} or type(scope['generation']) is not int or scope['generation']<0 or not SHA.fullmatch(str(scope['scope_fingerprint'])) or not isinstance(scope['instance_id'],str)):
            raise SharedError('共享范围身份无效，请重新核对目录。')
        return {'shares': self.shares(shares), 'expected_generation': scope['generation'], 'expected_scope_fingerprint': scope['scope_fingerprint']}

    def summary(self):
        value = self.request()
        shares = value.get('shares')
        if not isinstance(shares, list) or len(shares) > 12:
            raise SharedError('共享目录回执无效。')
        for item in shares:
            if not SHARE_ID.fullmatch(str(item.get('share_id', ''))) or type(item.get('version')) is not int:
                raise SharedError('共享目录身份无效。')
        return dict(self.scope(value), shares=[{k: item[k] for k in ('share_id', 'version', 'state')} for item in shares])

    def read(self, shares, scope):
        value = self.request('/read', self._payload(shares, scope))
        if self.scope(value) != scope:
            raise SharedError('共享范围已有变化，请重新预览。', 'shared_scope_changed')
        items = value.get('items')
        expected = {s['share_id']: s['expected_version'] for s in shares}
        if not isinstance(items, list) or len(items) != len(expected):
            raise SharedError('共享片段未完整回读，未发送。')
        safe, lines, chars = [], 0, 0
        for item in items:
            ident = item.get('share_id'); locator = item.get('locator', {}); text = item.get('text')
            if (ident not in expected or item.get('version') != expected.pop(ident)
                    or item.get('freshness') != 'verified' or not isinstance(text, str)
                    or not SHA.fullmatch(str(item.get('selected_sha256', '')))
                    or hashlib.sha256(text.encode()).hexdigest() != item['selected_sha256']
                    or locator.get('kind') != 'extracted_text_lines' or type(locator.get('line_start')) is not int or locator['line_start'] < 1
                    or type(locator.get('line_count')) is not int or not 1 <= locator['line_count'] <= 120
                    or len(text.split('\n')) != locator['line_count']
                    or any(not isinstance(item.get(k), str) or not 0 < len(item[k]) <= maximum for k, maximum in (('title',220),('source_kind',64),('line_reference',220)))):
                raise SharedError('共享正文、版本或位置回执不一致，未发送。')
            lines += locator['line_count']; chars += len(text)
            safe.append({key: copy.deepcopy(item[key]) for key in ('share_id','version','selected_sha256','title','source_kind','line_reference','locator','text','freshness')})
        if lines > 120 or chars > 12000:
            raise SharedError('共享片段超过本轮 120 行或 12000 字符，请缩小范围。')
        return sorted(safe, key=lambda x: x['share_id'])

    def search(self, query, limit, shares, scope):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or type(limit) is not int or not 1 <= limit <= 12:
            raise SharedError('共享检索词须为 1 至 200 字，结果最多 12 项。')
        result = self.request('/search', dict(self._payload(shares, scope), query=query.strip(), limit=limit))
        if self.scope(result) != scope or result.get('scope') != 'explicit_shared_only' or result.get('mode') != 'selected_literal':
            raise SharedError('共享检索范围不一致，请重新预览。', 'shared_scope_changed')
        expected = {s['share_id']: s['expected_version'] for s in shares}
        values = result.get('items', [])
        if not isinstance(values, list) or len(values) > limit:
            raise SharedError('共享搜索回执无效。')
        safe = []
        for item in values:
            if item.get('share_id') not in expected or item.get('version') != expected.pop(item['share_id']) or not isinstance(item.get('excerpt'), str) or len(item['excerpt']) > 850:
                raise SharedError('共享检索返回了未确认的片段。')
            safe.append({k: item[k] for k in ('share_id','version','selected_sha256','title','source_kind','line_reference','locator','excerpt','truncated') if k in item})
        return dict(self.scope(result), results=safe, mode='selected_literal', scope='explicit_shared_only',
                    result_count=result.get('result_count'), returned_count=len(safe), truncated=result.get('truncated', False))

    def open(self, share, scope):
        self.shares([share])
        result = self.request('/open', {'share_id':share['share_id'], 'expected_version':share['expected_version'],
                                     'expected_generation':scope['generation'], 'expected_scope_fingerprint':scope['scope_fingerprint']})
        if self.scope(result) != scope or result.get('opened') is not True:
            raise SharedError('原件未取得打开回执，请重试。')
        return {'opened':True}

    def preview(self, binding, shares, scope):
        items = self.read(shares, scope)
        preview = dict(self.scope(scope), shares=self.shares(shares), items=items, binding=copy.deepcopy(binding), binding_sha256=digest(binding))
        preview['preview_sha256'] = digest(preview)
        preview['preview_id'] = secrets.token_urlsafe(24)
        preview['expires_at'] = time.time() + 300
        with self.lock:
            self.previews = {k:v for k,v in self.previews.items() if v['expires_at'] > time.time()}
            if len(self.previews) >= 64:
                raise SharedError('待确认预览过多，请稍后重试。')
            self.previews[preview['preview_id']] = preview
        return {key:copy.deepcopy(value) for key,value in preview.items() if key!='binding'}

    def check(self, ident, checksum=None, binding=None, confirm=False):
        if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}',ident):
            raise SharedError('共享预览身份无效，请重新预览。')
        if confirm and (not isinstance(checksum,str) or not SHA.fullmatch(checksum) or binding is None):
            raise SharedError('共享确认摘要无效，请重新预览。')
        with self.lock:
            saved = self.previews.get(ident)
            if not saved or saved['expires_at'] <= time.time():
                raise SharedError('共享预览已过期或已使用，请重新预览；问题保留。', 'shared_preview_expired')
            preview = copy.deepcopy(saved)
        if callable(binding): binding = binding(copy.deepcopy(preview['binding']))
        if checksum is not None and checksum != preview['preview_sha256'] or binding is not None and digest(binding) != preview['binding_sha256']:
            raise SharedError('问题、模型或资料范围已改变，请重新预览。', 'shared_preview_changed')
        if self.read(preview['shares'], self.scope(preview)) != preview['items']:
            raise SharedError('共享片段已有变化，未发送；请重新预览。', 'shared_preview_changed')
        with self.lock:
            current = self.previews.get(ident)
            if not current or current['expires_at'] <= time.time():
                raise SharedError('共享预览已失效，未发送。', 'shared_preview_expired')
            if confirm:
                if current.get('confirmed'): raise SharedError('共享预览已确认使用，请重新预览。')
                current['confirmed'] = True
            elif not current.get('confirmed'):
                raise SharedError('本轮共享片段尚未确认，未发送。')
        return preview

    def finish(self, ident):
        with self.lock:
            self.previews.pop(ident, None)

    @staticmethod
    def boundary(preview):
        return dict(SharedKnowledge.scope(preview), shares=[{k:v for k,v in item.items() if k not in ('text','freshness')}
                    for item in preview['items']])
