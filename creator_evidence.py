"""Read two registered creator record kinds without starting or modifying its store."""
from contextlib import closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
SHA = re.compile(r'[0-9a-f]{64}')
KINDS = ('projects', 'publication_records')
MAX_RECORDS = 1000
MAX_DATABASE_BYTES = 64_000_000
MAX_PAYLOAD_BYTES = 1_000_000
MAX_KIND_BYTES = 4_000_000


def _identity(path, directory=False):
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('绑定路径无效')
    info = path.stat()
    if info.st_uid != os.getuid() or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('绑定文件身份不符')
    return info


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _json_pin(path, checksum, maximum):
    before = _identity(path)
    if before.st_size > maximum:
        raise ValueError('绑定文件过大')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
        if _stamp(os.fstat(handle.fileno())) != _stamp(before):
            raise ValueError('绑定文件已变化')
        raw = handle.read(maximum + 1)
    if len(raw) > maximum or hashlib.sha256(raw).hexdigest() != checksum or _stamp(_identity(path)) != _stamp(before):
        raise ValueError('绑定文件已变化')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('绑定结构无效')
    return value, _stamp(before)


def _sidecars(path):
    # Even mode=ro may create SQLite WAL coordination files; this bridge rejects WAL.
    if any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink() for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('数据库正在使用协调文件，暂不读取')
    with path.open('rb') as handle:
        header = handle.read(100)
    if len(header) != 100 or header[:16] != b'SQLite format 3\0' or header[18:20] != b'\x01\x01':
        raise ValueError('数据库日志模式暂不支持只读接入')


def _text(value, maximum=220):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError('记录字段无效')
    return value


def _date(value):
    value = _text(value, 80)
    if not value:
        return None
    moment = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TZ)
    if moment > dt.datetime.now(TZ):
        raise ValueError('记录日期在未来')
    return value


def _time(value):
    if not value:
        return float('-inf')
    moment = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (moment if moment.tzinfo else moment.replace(tzinfo=TZ)).timestamp()


def _projection(value, kind):
    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not value['id'] or len(value['id']) > 200:
        raise ValueError('记录身份无效')
    created, updated = _date(value.get('created_at')), _date(value.get('updated_at'))
    row = {'id': value['id'], 'title': _text(value.get('title')) or '未命名作品',
           'status': _text(value.get('status'), 120) or '待核', 'created_at': created, 'updated_at': updated}
    if kind == 'projects':
        row.update(at=updated or created, at_kind='作品更新' if updated else '作品创建', source='创宇台·已绑定本地作品记录')
    else:
        published, reported, recorded = (_date(value.get(k)) for k in ('published_at', 'reported_date', 'recorded_at'))
        row.update(at=published or reported, at_kind='发布发生' if published else '本人报告日期' if reported else '发生日期未登记',
                   published_at=published, reported_date=reported, recorded_at=recorded,
                   project_id=_text(value.get('project_id'), 200), platform=_text(value.get('platform'), 80),
                   evidence_level=_text(value.get('evidence_level'), 120),
                   phase='published' if row['status'] in ('已发布', 'published') else 'unconfirmed',
                   reported_by_user=value.get('reported_by_user') is True,
                   platform_verified=value.get('platform_verified') is True,
                   public_readback_verified=value.get('public_readback_verified') is True,
                   live_verified=False, source='创宇台·本地发布登记（非本次平台核验）')
        url = _text(value.get('url'), 2000)
        row['url'] = url if re.match(r'^https?://', url, re.I) else ''
        row['post_id'] = _text(value.get('post_id'), 200)
    return row


def read_creator_evidence(binding):
    result = {'status': 'unavailable', 'identity_verified': False, 'read_at': None,
              'source_updated_at': None, 'latest_record_at': None, 'works': [], 'publications': [],
              'coverage': {'kinds': list(KINDS), 'complete': False}, 'warnings': []}
    try:
        if (not isinstance(binding, dict) or not isinstance(binding.get('release_path'), str)
                or any(not SHA.fullmatch(str(binding.get(k, ''))) for k in ('release_sha256', 'registration_sha256'))
                or not isinstance(binding.get('workspace_id'), str) or not 1 <= len(binding['workspace_id']) <= 100):
            raise ValueError('正式绑定参数无效')
        release_path = Path(binding['release_path'])
        release, release_stamp = _json_pin(release_path, binding['release_sha256'], 4_000_000)
        workspace = release.get('workspace', {})
        if (release.get('app_id') != 'local.an.workbench' or release.get('purpose') != 'production'
                or not isinstance(workspace, dict) or workspace.get('registration_sha256') != binding['registration_sha256']
                or not isinstance(workspace.get('registration_path'), str)):
            raise ValueError('正式应用身份不符')
        registration_path = Path(workspace['registration_path'])
        registration, registration_stamp = _json_pin(registration_path, binding['registration_sha256'], 128_000)
        if (registration.get('schema') != 2 or registration.get('purpose') != 'production'
                or registration.get('workspace_id') != binding['workspace_id'] or type(registration.get('database_schema')) is not int):
            raise ValueError('正式资料库身份不符')
        root = Path(registration['workspace_root'])
        directories = registration.get('directories', {})
        if registration_path != root / 'data/native-registration.json':
            raise ValueError('正式登记文件位置不符')
        paths = [(root, registration.get('workspace_identity')), (root / 'data', directories.get('data')),
                 (root / 'data/desktop-state', directories.get('desktop-state'))]
        if registration.get('state_root') != str(root / 'data/desktop-state'):
            raise ValueError('正式资料目录不符')
        dir_stamps = []
        for path, expected in paths:
            info = _identity(path, directory=True)
            if [info.st_dev, info.st_ino] != expected:
                raise ValueError('正式目录身份已变化')
            dir_stamps.append((path, info.st_dev, info.st_ino, info.st_uid, info.st_mode))
        database = root / 'data/state.sqlite3'
        before = _identity(database)
        if [before.st_dev, before.st_ino] != registration.get('database_identity'):
            raise ValueError('正式数据库已替换')
        _sidecars(database)
        if before.st_size > MAX_DATABASE_BYTES:
            raise ValueError('正式数据库超过本次只读快照上限')
        with os.fdopen(os.open(database, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
            if _stamp(os.fstat(handle.fileno())) != _stamp(before):
                raise ValueError('正式数据库已变化')
            raw = handle.read(MAX_DATABASE_BYTES + 1)
        if len(raw) > MAX_DATABASE_BYTES or _stamp(_identity(database)) != _stamp(before):
            raise ValueError('读取期间来源发生变化')
        _sidecars(database)
        items, gaps = {}, []
        # SQLite only opens our disposable copy, so a source WAL-mode race cannot create source sidecars.
        with tempfile.TemporaryDirectory(prefix='ruigu-creator-') as temporary:
            snapshot = Path(temporary) / 'snapshot.sqlite3'
            with os.fdopen(os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as handle:
                handle.write(raw)
            with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=2)) as connection:
                deadline = time.monotonic() + 2
                connection.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                if connection.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':
                    raise ValueError('数据库日志模式暂不支持')
                if connection.execute('PRAGMA user_version').fetchone()[0] != registration['database_schema']:
                    raise ValueError('数据库版本不符')
                metadata = dict(connection.execute("SELECT key,value FROM metadata WHERE key IN ('purpose','workspace_id')"))
                if metadata != {'purpose': 'production', 'workspace_id': binding['workspace_id']}:
                    raise ValueError('数据库内部身份不符')
                for kind in KINDS:
                    count, total, maximum = connection.execute('SELECT count(*),coalesce(sum(length(CAST(payload AS BLOB))),0),coalesce(max(length(CAST(payload AS BLOB))),0) FROM records WHERE kind=?', (kind,)).fetchone()
                    if count > MAX_RECORDS or total > MAX_KIND_BYTES or maximum > MAX_PAYLOAD_BYTES:
                        gaps.append(kind); items[kind] = []; continue
                    rows = connection.execute('SELECT id,payload FROM records WHERE kind=? LIMIT ?', (kind, MAX_RECORDS)).fetchall()
                    parsed = []
                    for ident, raw in rows:
                        try:
                            if not isinstance(raw, str) or len(raw) > MAX_PAYLOAD_BYTES:
                                raise ValueError()
                            value = json.loads(raw)
                            if not isinstance(value, dict) or value.get('id') != ident:
                                raise ValueError()
                            parsed.append(value)
                        except (ValueError, TypeError):
                            gaps.append(kind)
                    items[kind] = parsed
        _sidecars(database)
        if (_stamp(_identity(database)) != _stamp(before) or _stamp(_identity(release_path)) != release_stamp
                or _stamp(_identity(registration_path)) != registration_stamp):
            raise ValueError('读取期间来源发生变化')
        for path, dev, ino, uid, mode in dir_stamps:
            info = _identity(path, directory=True)
            if (info.st_dev, info.st_ino, info.st_uid, info.st_mode) != (dev, ino, uid, mode):
                raise ValueError('读取期间目录身份变化')
        excluded = {v.get('id') for v in items['projects'] if v.get('synthetic') or v.get('purpose') in ('test', 'acceptance', 'fixture')}
        seen = set()
        for kind in KINDS:
            for value in items[kind]:
                if value.get('synthetic') or value.get('purpose') in ('test', 'acceptance', 'fixture') or value.get('project_id') in excluded:
                    continue
                try:
                    row = _projection(value, kind)
                    if kind == 'publication_records' and row['phase'] == 'published' and not row['at']:
                        gaps.append(kind)
                    if kind == 'publication_records':
                        key = (row['platform'], row['post_id'] or row['url'] or row['id'])
                        if key in seen:
                            continue
                        seen.add(key)
                    result['works' if kind == 'projects' else 'publications'].append(row)
                except (ValueError, TypeError):
                    gaps.append(kind)
        dates = [row.get(k) for group in ('works', 'publications') for row in result[group]
                 for k in ('updated_at', 'created_at', 'recorded_at') if row.get(k)]
        latest = max(dates, key=_time) if dates else None
        result.update(status='partial' if gaps else 'connected', identity_verified=True,
                      read_at=dt.datetime.now(TZ).isoformat(), latest_record_at=latest, source_updated_at=latest,
                      coverage={'kinds': list(KINDS), 'complete': not gaps, 'incomplete_kinds': sorted(set(gaps)),
                                'note': '仅含已绑定正式库的作品与发布登记；未扫描素材、其他工作区或实时平台。'})
        if gaps:
            result['warnings'].append('部分登记记录过量或格式无效，未完整统计；缺失不表示没有创作。')
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        result.update(works=[], publications=[])
        result['warnings'].append('创宇台正式来源暂未通过身份、版本或只读检查；未使用旧端口或推定发布数量，请重新核对本机绑定。')
    return result
