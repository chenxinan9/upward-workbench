"""Private, session-bound uploads. Documents are data and are never executed."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
import zipfile
import xml.etree.ElementTree as ET
import warnings
import selectors
import time

MAX_BYTES = 20 * 1024 * 1024
TEXT_EXT = {'.txt', '.md', '.csv', '.json', '.log', '.yaml', '.yml', '.py', '.js', '.html', '.css'}
IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
DOCUMENT_EXT = {'.pdf', '.docx', '.xlsx', '.pptx'}


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)


def _office(data, suffix):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) > 3000 or sum(x.file_size for x in archive.infolist()) > 50 * 1024 * 1024:
            raise ValueError('Office 文档解压体积过大')
        prefix = {'.docx':'word/', '.xlsx':'xl/', '.pptx':'ppt/slides/'}[suffix]
        chosen = sorted((n for n in names if n.startswith(prefix) and n.endswith('.xml')
                        and ('document.xml' in n or 'header' in n or 'footer' in n or
                             'sharedStrings.xml' in n or '/worksheets/' in n or '/slide' in n)),
                        key=lambda n:[int(v) if v.isdigit() else v for v in re.split(r'(\d+)',n)])
        if not chosen:
            raise ValueError('无法识别文档结构')
        parts = []; trees = {}
        for name in chosen:
            raw = archive.read(name)
            normalized = raw.replace(b'\x00', b'').upper()
            if b'<!DOCTYPE' in normalized or b'<!ENTITY' in normalized:
                raise ValueError('不接受含 XML 实体的文档')
            tree = ET.fromstring(raw)
            trees[name] = tree
        strings = []
        if suffix == '.xlsx':
            shared = trees.get('xl/sharedStrings.xml')
            if shared is not None:
                strings = [''.join(e.text or '' for e in item.iter() if e.tag.rsplit('}',1)[-1]=='t') for item in shared]
        for name, tree in trees.items():
            if suffix == '.xlsx':
                if '/worksheets/' not in name: continue
                cells=[]
                for cell in tree.iter():
                    if cell.tag.rsplit('}',1)[-1]!='c': continue
                    values={e.tag.rsplit('}',1)[-1]:e.text or '' for e in cell}
                    value=values.get('v','')
                    if cell.get('t')=='s':
                        try: value=strings[int(value)]
                        except (ValueError,IndexError): value='[无效共享字符串索引]'
                    elif cell.get('t')=='inlineStr': value=''.join(e.text or '' for e in cell.iter() if e.tag.rsplit('}',1)[-1]=='t')
                    if 'f' in values: value+=' [公式: '+values['f']+'；结果为文件缓存值，未重算]'
                    cells.append(cell.get('r','?')+': '+value)
                parts.append('['+name+']\n'+'\n'.join(cells)); continue
            text = '\n'.join(e.text for e in tree.iter() if e.tag.rsplit('}', 1)[-1] in ('t', 'v') and e.text)
            parts.append('[' + name + ']\n' + text)
        return '\n\n'.join(parts), '提取正文与单元格；公式未重算，图表、嵌入图片与版式请结合原文件核对'


class AttachmentStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve() / 'workspace-attachments'
        if self.root.is_symlink():
            raise ValueError('附件目录不能是符号链接')
        self.root.mkdir(mode=0o700, exist_ok=True)

    def _folder(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise ValueError('附件标识无效')
        folder = self.root / identifier
        if self.root.is_symlink() or folder.is_symlink() or not folder.is_dir():
            raise ValueError('附件不存在或路径异常')
        return folder

    def _load(self, identifier):
        folder = self._folder(identifier)
        path = folder / 'metadata.json'
        if path.is_symlink():
            raise ValueError('附件元数据路径异常')
        return json.loads(path.read_text())

    @staticmethod
    def public(meta):
        return {k:meta[k] for k in ('id','name','kind','bytes','status','sha256','note','text_characters') if k in meta}

    def upload(self, name, encoded):
        if not isinstance(name, str) or not name.strip() or len(name) > 180 or any(c in name for c in '/\\\x00'):
            raise ValueError('附件名称无效')
        suffix = Path(name).suffix.lower()
        if suffix not in TEXT_EXT | IMAGE_EXT | DOCUMENT_EXT:
            raise ValueError('支持图片、PDF、Word、Excel、PPT 和文本；音视频请先转成文字')
        if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES * 4 // 3 + 8):
            raise ValueError('单个附件不能超过 20 MB')
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ValueError('附件编码无效') from None
        if not 0 < len(data) <= MAX_BYTES:
            raise ValueError('附件为空或超过 20 MB')
        if sum(p.stat().st_size for p in self.root.glob('*/source*') if p.is_file() and not p.is_symlink()) + len(data) > 500 * 1024 * 1024:
            raise ValueError('本机附件已超过 500 MB，请先整理历史附件')
        identifier = uuid.uuid4().hex
        folder = self.root / identifier
        folder.mkdir(mode=0o700)
        source = folder / ('source' + suffix)
        try:
            _write(source, data)
            note = ''; text = ''
            if suffix in IMAGE_EXT:
                valid = (suffix == '.png' and data.startswith(b'\x89PNG\r\n\x1a\n') or
                         suffix in ('.jpg','.jpeg') and data.startswith(b'\xff\xd8\xff') or
                         suffix == '.gif' and data.startswith((b'GIF87a',b'GIF89a')) or
                         suffix == '.webp' and data[:4] == b'RIFF' and data[8:12] == b'WEBP')
                if not valid: raise ValueError('图片格式与扩展名不一致')
                try:
                    from PIL import Image
                except ImportError:
                    binary = shutil.which('sips')
                    if not binary: raise ValueError('图片校验需要 Pillow，请安装后重试：python3 -m pip install Pillow')
                    checked = subprocess.run([binary,'-g','pixelWidth','-g','pixelHeight',str(source)],capture_output=True,text=True,timeout=15)
                    dimensions = re.findall(r'pixel(?:Width|Height):\s*(\d+)',checked.stdout)
                    if checked.returncode or len(dimensions)!=2 or not 0<int(dimensions[0])*int(dimensions[1])<=40_000_000:
                        raise ValueError('图片损坏或像素超过 4000 万')
                else:
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter('error',Image.DecompressionBombWarning)
                            with Image.open(io.BytesIO(data)) as img:
                                if not 0<img.width*img.height<=40_000_000 or getattr(img,'n_frames',1)>200:
                                    raise ValueError('图片像素或帧数过大')
                                img.verify()
                            with Image.open(io.BytesIO(data)) as img: img.load()
                    except Exception:
                        raise ValueError('图片损坏、格式不支持或像素过大') from None
                kind = 'image'
            elif suffix == '.pdf':
                if not data.startswith(b'%PDF-'): raise ValueError('PDF 格式无效')
                binary = shutil.which('pdftotext')
                if not binary: raise ValueError('本机缺少 PDF 文字提取工具 pdftotext；可先导出为 TXT 或发送截图')
                process = subprocess.Popen([binary,'-f','1','-l','60','-enc','UTF-8',str(source),'-'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
                chunks=[];size=0;truncated=False;deadline=time.monotonic()+25
                selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
                try:
                    while True:
                        if time.monotonic()>deadline: raise ValueError('PDF 文字提取超时')
                        if not selector.select(.1): continue
                        block=os.read(process.stdout.fileno(),65536)
                        if not block: break
                        remaining=1200000-size
                        chunks.append(block[:remaining]);size+=min(remaining,len(block))
                        if len(block)>remaining:
                            truncated=True;process.kill();break
                    code=process.wait(timeout=3)
                    if code and not truncated: raise ValueError('PDF 无法读取，可能加密或损坏')
                finally:
                    selector.close()
                    if process.poll() is None: process.kill()
                    process.wait();process.stdout.close()
                text = b''.join(chunks).decode('utf-8',errors='replace');kind = 'document'
                note = '提取前 60 页可选文字；扫描页和图片未做 OCR，不能视为已读全部内容'
                if truncated: note += '；提取输出达到 120 万字节上限，已停止并截断'
                if not text.strip(): raise ValueError('PDF 没有可提取文字，请发送截图或先做 OCR')
            elif suffix in DOCUMENT_EXT:
                text,note = _office(data,suffix);kind = 'document'
            else:
                kind = 'text'
                try: text = data.decode('utf-8-sig')
                except UnicodeDecodeError:
                    try: text = data.decode('gb18030')
                    except UnicodeDecodeError: raise ValueError('文本编码无法识别，请转成 UTF-8') from None
                if '\x00' in text: raise ValueError('附件不是可识别文本')
            if len(text) > 300000:
                text = text[:300000];note += '；文字超过上限，仅保留前 30 万字符'
            _write(folder/'text.txt',text.encode())
            meta = {'id':identifier,'name':name,'kind':kind,'bytes':len(data),'status':'ready',
                    'sha256':hashlib.sha256(data).hexdigest(),'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
                    'text_characters':len(text),'note':note,'suffix':suffix,'session_id':None}
            _write(folder/'metadata.json',(json.dumps(meta,ensure_ascii=False)+'\n').encode())
            return self.public(meta)
        except BaseException:
            shutil.rmtree(folder)
            raise

    def bind(self, identifiers, session_id):
        if not isinstance(identifiers,list) or len(identifiers)>8 or len(set(identifiers))!=len(identifiers):
            raise ValueError('每次最多 8 个不同附件')
        metas = [self._load(i) for i in identifiers]
        if any(m['session_id'] not in (None,session_id) for m in metas):
            raise PermissionError('附件属于另一段对话，请重新选择上传')
        for meta in metas:
            if meta['session_id'] is None:
                meta['session_id']=session_id;folder=self._folder(meta['id'])
                temp=folder/('metadata-'+uuid.uuid4().hex+'.tmp')
                _write(temp,(json.dumps(meta,ensure_ascii=False)+'\n').encode());os.replace(temp,folder/'metadata.json')
        return [self.public(m) for m in metas]

    def read(self, identifier, session_id, offset=0, limit=120):
        meta=self._load(identifier)
        if meta['session_id']!=session_id: raise PermissionError('附件不属于本会话')
        folder=self._folder(identifier);path=folder/'text.txt'
        if path.is_symlink(): raise ValueError('附件路径异常')
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=meta['text_sha256']:raise ValueError('附件已被外部修改，请重新上传')
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=300:raise ValueError('附件行范围无效')
        lines=data.decode().splitlines();chunk='\n'.join(lines[offset:offset+limit])
        if len(chunk)>80000:raise ValueError('附件行过长，请缩小读取范围或先拆分文件')
        return dict(self.public(meta),text=chunk,total_lines=len(lines),line=offset+1,
                    next_offset=offset+limit if offset+limit<len(lines) else None)

    def image_inputs(self, identifiers, session_id):
        items=[]
        for identifier in identifiers:
            meta=self._load(identifier)
            if meta['session_id']!=session_id:raise PermissionError('附件不属于本会话')
            if meta['kind']!='image':continue
            path=self._folder(identifier)/('source'+meta['suffix'])
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=meta['sha256']:raise ValueError('附件已被外部修改')
            items.append({'type':'localImage','path':str(path)})
        return items
