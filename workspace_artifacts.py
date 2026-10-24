"""Private, job-bound reply artifacts. No discovery of model-mentioned paths."""
from contextlib import contextmanager
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from vault import VaultWorkspace

FORMATS = {'markdown':'md', 'text':'txt', 'html':'html', 'json':'json', 'csv':'csv', 'svg':'svg', 'code':'txt'}
MAX_BYTES = 256 * 1024
MAX_FILES = 4
PREVIEW_CSP = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; font-src 'none'; media-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('会话成果标识无效')
    return value


def _style(value):
    value = re.sub(r'/\*[\s\S]*?\*/', '', value)
    if re.search(r'url\s*\(|expression\s*\(|behavior\s*:|[\\@<>]', value, re.I):
        return ''
    return value



class _StaticHTML(HTMLParser):
    tags = set('html head body title style div span p h1 h2 h3 h4 h5 h6 ul ol li section article header footer main aside nav figure figcaption table thead tbody tfoot tr th td pre code blockquote strong em b i u s small hr br a'.split())
    blocked_tags = set('script iframe frame frameset object embed form textarea button input select option template svg math video audio canvas'.split())
    voids = {'br','hr','img','meta','base','link','input','embed','source','track','wbr'}
    attrs = {'class','id','title','role','aria-label','colspan','rowspan','width','height','alt'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []; self.blocked = []; self.in_style = False

    def handle_starttag(self, tag, attrs):
        if self.blocked:
            if tag not in self.voids: self.blocked.append(tag)
            return
        if tag in self.blocked_tags:
            if tag not in self.voids: self.blocked.append(tag)
            return
        if tag not in self.tags: return
        safe = []
        for name, value in attrs:
            if not isinstance(value, str): continue
            if name == 'style': value = _style(value)
            elif name not in self.attrs: continue
            if value: safe.append(name+'="'+escape(value, quote=True)+'"')
        self.parts.append('<'+tag+(' '+' '.join(safe) if safe else '')+'>')
        if tag == 'style': self.in_style = True

    def handle_endtag(self, tag):
        if self.blocked:
            if tag in self.blocked:
                self.blocked = self.blocked[:self.blocked.index(tag)]
            return
        if tag in self.tags and tag not in self.voids:
            self.parts.append('</'+tag+'>')
        if tag == 'style': self.in_style = False

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(escape(_style(data) if self.in_style else data, quote=False))


def static_preview(content):
    parser = _StaticHTML()
    parser.feed(content); parser.close()
    return ('<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'+
            escape(PREVIEW_CSP, quote=True)+'"></head><body>'+''.join(parser.parts)+'</body></html>')


class WorkspaceArtifacts:
    def __init__(self, store):
        self.store = store
        self.root = store.path / 'workspace-artifacts'
        self.lock = threading.RLock()

    def _job(self, job_id, session_id):
        _identifier(job_id); _identifier(session_id)
        job = self.store.get(job_id, 'workspace-job')
        if job.get('session_id') != session_id:
            raise ValueError('会话成果不属于当前会话')
        return job

    @staticmethod
    def public(value):
        return {k:value[k] for k in ('id','title','format','sha','bytes','status','job_id','session_id') if k in value}

    def list(self, job_id, session_id):
        self._job(job_id, session_id)
        with self.store.conn() as connection:
            rows = connection.execute("SELECT id,data FROM objects WHERE kind='workspace-artifact' AND json_extract(data,'$.job_id')=? AND json_extract(data,'$.session_id')=? ORDER BY rowid LIMIT ?", (job_id,session_id,MAX_FILES+1)).fetchall()
        return [dict(json.loads(row['data']),id=row['id']) for row in rows]

    @contextmanager
    def _folder(self, session_id, job_id, create=False):
        _identifier(session_id); _identifier(job_id)
        with VaultWorkspace._directory_fd(self.root/session_id/job_id, create=create) as directory:
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError('会话成果目录身份无效')
            yield directory

    def read(self, job_id, session_id, artifact_id):
        self._job(job_id, session_id); _identifier(artifact_id)
        value = self.store.get(artifact_id, 'workspace-artifact')
        if (value.get('job_id') != job_id or value.get('session_id') != session_id or
                value.get('status') != 'verified' or value.get('format') not in FORMATS):
            raise ValueError('会话成果尚未核验或不属于本轮')
        with self._folder(session_id,job_id) as directory:
            name = artifact_id+'.'+FORMATS[value['format']]
            descriptor = os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            with os.fdopen(descriptor,'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                        stat.S_IMODE(info.st_mode)&0o077 or info.st_size > MAX_BYTES):
                    raise ValueError('会话成果文件身份或大小无效')
                raw = stream.read(MAX_BYTES+1)
        if len(raw) != value.get('bytes') or hashlib.sha256(raw).hexdigest() != value.get('sha'):
            raise ValueError('会话成果已有变化，未展示未经核验的内容')
        result = dict(self.public(value), text=raw.decode('utf-8'))
        if value['format'] == 'html': result['preview_html'] = static_preview(result['text'])
        return result

    def create(self, job_id, session_id, title, format, content):
        job = self._job(job_id,session_id)
        if job.get('status') not in ('queued','running'):
            raise ValueError('本轮已结束，不再创建会话成果')
        if (not isinstance(title,str) or not 1<=len(title.strip())<=120 or re.search(r'[\x00-\x1f\x7f-\x9f\u2028-\u202e]',title) or
                not isinstance(format,str) or format not in FORMATS or not isinstance(content,str) or not content.strip() or '\x00' in content):
            raise ValueError('会话成果标题、格式或正文无效')
        raw = content.encode('utf-8')
        if len(raw) > MAX_BYTES: raise ValueError('每份会话成果最多256KiB，请先精简')
        if format == 'json':
            try: json.dumps(json.loads(content),allow_nan=False)
            except (ValueError,RecursionError): raise ValueError('成果不是有效的有限JSON') from None
        checksum = hashlib.sha256(raw).hexdigest()
        key = hashlib.sha256(json.dumps([title.strip(),format,checksum],ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            previous = self.list(job_id,session_id)
            same = next((v for v in previous if v.get('request_key') == key),None)
            if same:
                return self.public(self.read(job_id,session_id,same['id']))
            if len(previous) >= MAX_FILES: raise ValueError('每轮最多4份会话成果，请继续使用已有成果')
            identifier = uuid.uuid4().hex
            self.store.put('workspace-artifact',{'job_id':job_id,'session_id':session_id,'title':title.strip(),
                'format':format,'bytes':len(raw),'sha':checksum,'request_key':key,'status':'started'},id=identifier)
            try:
                with self._folder(session_id,job_id,create=True) as directory:
                    descriptor = os.open(identifier+'.'+FORMATS[format],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
                    with os.fdopen(descriptor,'wb') as stream:
                        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                    checked = VaultWorkspace._bytes_at(directory,identifier+'.'+FORMATS[format])
                    if checked != raw:
                        raise ValueError('会话成果回读不一致，未认定保存成功')
                self.store.change(identifier,'workspace-artifact',status='verified')
                verified = self.read(job_id,session_id,identifier)
            except Exception:
                self.store.change(identifier,'workspace-artifact',status='uncertain')
                raise
            return self.public(verified)
