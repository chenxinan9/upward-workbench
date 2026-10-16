"""Read only model metadata already connected to the local Codex installation.

Catalog presence is configuration evidence, never a promise of provider health.
No credentials, provider URLs, prompts or full configuration are returned.
"""
import copy
import hashlib
import threading
import datetime as dt
import json
import os
from pathlib import Path
import re
import tomllib
import time
import shutil
from urllib.parse import urlsplit

_MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}\Z')
_EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
_MAX_METADATA_BYTES = 12 * 1024 * 1024
_RECEIPT_LOCK = threading.RLock()
_DIRECTORY_LOCK = threading.RLock()


def _read(path, kind):
    try:
        if not path.is_file() or path.stat().st_size > _MAX_METADATA_BYTES:
            return {}
        value = (tomllib.loads if kind == 'toml' else json.loads)(path.read_text())
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError, UnicodeError):
        return {}


def _label(value, fallback):
    if not isinstance(value, str):
        return fallback
    # Model metadata remains untrusted text; the UI must still HTML-escape it.
    value = ''.join(c for c in value if c.isprintable()).strip()
    return value[:120] or fallback


def _normalize(raw, source, provider):
    if not isinstance(raw, dict) or raw.get('visibility') == 'hide' or raw.get('hidden') is True:
        return None
    ident = raw.get('model') or raw.get('slug') or raw.get('id')
    if not isinstance(ident, str) or not _MODEL_ID.fullmatch(ident):
        return None
    levels = raw.get('supportedReasoningEfforts', raw.get('supported_reasoning_levels', []))
    if not isinstance(levels, list):
        levels = []
    supported = []
    for level in levels:
        if isinstance(level, dict):
            effort = level.get('reasoningEffort', level.get('effort'))
            if effort in _EFFORTS and effort not in supported:
                supported.append(effort)
    default = raw.get('defaultReasoningEffort', raw.get('default_reasoning_level'))
    if default not in supported:
        default = next((e for e in ('high', 'medium', 'low') if e in supported), supported[0] if supported else None)
    return {
        'id': ident,
        'label': _label(raw.get('displayName', raw.get('display_name')), ident),
        'provider': ident.split('/')[0] if '/' in ident else provider,
        'source': source,
        'supported_reasoning_efforts': supported,
        'reasoning_efforts': list(supported),
        'default_reasoning_effort': default,
        'verified': False,
        'verification': '尚未试答',
        'is_default': raw.get('isDefault') is True,
    }


class ModelAccessMixin:
    def _directory_key(self):
        home, config, overlay = self._model_configuration()
        cli = shutil.which(str(getattr(self,'cli','') or '')) or str(getattr(self,'cli','') or '')
        paths = [home/'config.toml',home/'opencodex.config.toml',home/'auth.json']
        path = config.get('model_catalog_json') or overlay.get('model_catalog_json')
        if isinstance(path,str) and path:
            path = Path(path).expanduser(); paths.append(path if path.is_absolute() else home/path)
        if cli: paths.append(Path(cli).resolve())
        identity = []
        for path in paths:
            try:
                value = path.stat(); signature = [value.st_dev,value.st_ino,value.st_mtime_ns,value.st_size]
            except OSError: signature = None
            identity.append([str(path),signature])
        return hashlib.sha256(json.dumps([cli,identity]).encode()).hexdigest()

    def _native_directory(self, *, refresh=False):
        from codex_workspace import read_model_directory
        with _DIRECTORY_LOCK:
            key = self._directory_key()
            cached = getattr(self,'_model_directory_cache',None)
            if not refresh and cached and cached['key']==key and time.monotonic()-cached['at']<30:
                return copy.deepcopy(cached)
            entry = {'key':key,'at':time.monotonic(),'models':[],'error':''}
            try:
                home, _, _ = self._model_configuration()
                metadata = read_model_directory(getattr(self,'cli',None),home=home)
                rows = [_normalize(row,'当前Codex账户目录','Codex') for row in metadata['models']]
                entry['models'] = [row for row in rows if row]
                if not entry['models']: raise ValueError('当前Codex目录没有提供模型，请保留选择后刷新')
                if key != self._directory_key(): raise ValueError('读取期间账户或配置发生变化，请重新刷新模型目录')
            except (ValueError,OSError) as exc:
                entry.update(models=[],error=str(exc))
            self._model_directory_cache = entry
            return copy.deepcopy(entry)

    def _model_configuration(self):
        home = Path(getattr(self, 'model_home', os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))))
        config = _read(home / 'config.toml', 'toml')
        overlay = _read(home / 'opencodex.config.toml', 'toml')
        return home, config, overlay

    def _configured_catalog(self):
        """Return a fresh, small whitelist projection; never expose raw config."""
        home, config, overlay = self._model_configuration()
        provider = _label(config.get('model_provider'), 'Codex')
        configured = config.get('model')
        if not isinstance(configured, str) or not _MODEL_ID.fullmatch(configured):
            configured = ''
        models, source, source_time = [], '', None
        # The local CLI merges opencodex's catalog. Prefer the explicit catalog
        # over a native-model cache which may omit connected proxy providers.
        catalog_path = config.get('model_catalog_json') or overlay.get('model_catalog_json')
        if isinstance(catalog_path, str) and catalog_path:
            path = Path(catalog_path).expanduser()
            if not path.is_absolute():
                path = home / path
            catalog = _read(path, 'json')
            rows = catalog.get('models', [])
            if isinstance(rows, list):
                source = '本机 Codex 配置目录'
                models = [_normalize(row, source, provider) for row in rows[:300]]
                models = [m for m in models if m]
            if models:
                try:
                    source_time = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat()
                except OSError:
                    pass
        # A cache belonging to a different explicit provider is not its allowlist.
        if not models and config.get('model_provider') in (None, '', 'openai'):
            cached = _read(home / 'models_cache.json', 'json')
            rows = cached.get('models', [])
            if isinstance(rows, list):
                source = '本机 Codex 缓存目录'
                models = [_normalize(row, source, provider) for row in rows[:300]]
                models = [m for m in models if m]
            if isinstance(cached.get('fetched_at'), str):
                source_time = cached['fetched_at'][:80]
        by_id = {m['id']: m for m in models}
        if configured and configured not in by_id:
            # The configured default itself is evidence even when list discovery
            # is unavailable, but it is explicitly not a successful-call receipt.
            m = _normalize({'slug': configured}, '本机 Codex 当前配置', provider)
            by_id[configured] = m
        models = list(by_id.values())
        models = [m for m in models if m['id'].startswith('gpt-') or m['id'].startswith('gemini-web/')]
        for model in models:
            model['is_default'] = model['id'] == configured
        return {
            'default_id': configured,
            'default_model': configured,
            'status': 'ready' if models else 'unavailable',
            'models': models,
            'source': source or ('本机 Codex 当前配置' if configured else '未发现模型目录'),
            'source_updated_at': source_time,
            'notice': '目录表示本机已配置的模型；是否能回答取决于当前登录、额度与服务状态。选择只影响本轮，不改 Codex 全局设置。',
        }

    def configured_models(self):
        """Private probe candidates. Never use this list as the product selector."""
        return self._configured_catalog()['models']

    def _configuration_fingerprint(self, model):
        _, config, overlay = self._model_configuration()
        bindings = []
        urls = [overlay.get('openai_base_url'), config.get('openai_base_url')]
        providers = config.get('model_providers', {})
        if isinstance(providers, dict):
            provider = providers.get(config.get('model_provider'), {})
            if isinstance(provider, dict): urls.append(provider.get('base_url'))
        for value in urls:
            if isinstance(value, str):
                try:
                    u = urlsplit(value)
                    # No userinfo, credentials, query values or API keys enter this hash.
                    bindings.append([u.scheme, u.hostname, u.port, u.path])
                except ValueError:
                    bindings.append(['invalid-binding'])
        payload = {'id': model['id'], 'provider': model['provider'],
                   'efforts': model['supported_reasoning_efforts'], 'bindings': bindings}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _verification_path(self):
        store = getattr(self, 'store', None)
        root = getattr(store, 'path', None) or getattr(self, 'model_state_path', None)
        if root is None:
            raise ValueError('缺少工作台本地数据目录')
        return Path(root) / 'model-verification.json'

    def verify_model(self, ident, actual_model):
        """Call only after a completed probe/answer; configuration alone never verifies."""
        model = self.resolve_model(ident, require_verified=False)
        if not isinstance(actual_model, str) or actual_model != model['id']:
            raise ValueError('实际响应模型与验证目标不一致，未记录可用')
        receipt = {'id': model['id'], 'actual_model': actual_model,
                   'verified_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                   'configuration_fingerprint': self._configuration_fingerprint(model)}
        path = self._verification_path()
        with _RECEIPT_LOCK:
            state = _read(path, 'json')
            receipts = state.get('models', {})
            if not isinstance(receipts, dict): receipts = {}
            receipts[ident] = receipt
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps({'version': 1, 'models': receipts}, ensure_ascii=False))
            temp.chmod(0o600); temp.replace(path)
        return receipt

    def invalidate_model(self, ident):
        """Explicitly revoke a receipt after a confirmed model-access failure.

        Do not call for user cancellation, a rejected tool request or answer errors."""
        path = self._verification_path()
        with _RECEIPT_LOCK:
            state = _read(path, 'json')
            receipts = state.get('models', {})
            if not isinstance(receipts, dict) or ident not in receipts: return
            del receipts[ident]
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps({'version': 1, 'models': receipts}, ensure_ascii=False))
            temp.chmod(0o600); temp.replace(path)

    def model_catalog(self, *, refresh=False):
        key = self._directory_key()
        catalog = self._configured_catalog()
        _, config, _ = self._model_configuration()
        native = config.get('model_provider') in (None,'','openai')
        directory = self._native_directory(refresh=refresh) if native else None
        configured = {row['id']:row for row in catalog['models']}
        offered = {row['id']:row for row in directory['models']} if directory else {}
        models = list(offered.values()) + [row for ident,row in configured.items() if ident not in offered]
        try: state = _read(self._verification_path(), 'json')
        except ValueError: state = {}
        receipts = state.get('models', {})
        if not isinstance(receipts, dict): receipts = {}
        current = dt.datetime.now(dt.timezone.utc)
        for model in models:
            model.update(selectable=False,availability='not_provided',verification='当前目录未提供')
            receipt = receipts.get(model['id'], {})
            valid = False
            try:
                when = dt.datetime.fromisoformat(receipt['verified_at'])
                valid = (when.tzinfo is not None and when<=current and receipt.get('actual_model')==model['id']
                         and receipt.get('configuration_fingerprint')==self._configuration_fingerprint(model))
            except (KeyError,ValueError,TypeError): pass
            if valid: model.update(verified=True,verified_at=receipt['verified_at'])
            other_route = not native or model['id'].startswith('gemini-web/')
            if model['id'] in offered:
                model.update(selectable=True,availability='provided',verification='本机已回答核验' if valid else '当前账户目录提供；尚无独立连接检查回执，本轮回答状态以会话回执为准')
            elif other_route:
                model.update(selectable=valid,availability='verified_route' if valid else 'unverified_route',
                    verification='本机已回答核验' if valid else '独立路由尚未完成本机回答核验')
            elif directory['error']:
                model.update(availability='directory_unavailable',verification='目录暂不可读，保留选择待重核')
        default = (next((m['id'] for m in models if m.get('is_default') and m['id'] in offered),'') if native else
                   next((m['id'] for m in models if m['id']==catalog['default_id'] and m['selectable']),''))
        changed = key != self._directory_key() or directory is not None and directory['key'] != key
        if changed:
            for model in models:
                model.update(selectable=False,availability='directory_unavailable',verification='账户或配置读取期间变化，保留选择待重核')
            default = ''
        scope_rows = [{key:model.get(key) for key in ('id','supported_reasoning_efforts','selectable','is_default','availability')} for model in models]
        scope = hashlib.sha256(json.dumps([self._directory_key(),scope_rows],sort_keys=True).encode()).hexdigest()
        catalog.update(models=models,default_id=default,default_model=default,scope_fingerprint=scope,
            status='unavailable' if changed or directory and directory['error'] else 'ready' if any(m['selectable'] for m in models) else 'checking',
            directory_error='账户或配置已变化，请重新刷新目录' if changed else directory['error'] if directory else '',
            notice='此目录来自本工作台连接的Codex CLI与当前账户，其他客户端或入口可能不同。目录提供项可直接选择，尚未试答不代表不可选；回答核验只说明历史成功，选择不改全局配置。')
        return catalog

    def resolve_model(self, ident=None, require_verified=True):
        catalog = self.model_catalog()
        if ident is None or ident == '': ident = catalog['default_id']
        if not isinstance(ident, str) or not _MODEL_ID.fullmatch(ident):
            raise ValueError(catalog.get('directory_error') or '当前目录没有可用默认，请明确选择模型')
        model = next((m for m in catalog['models'] if m['id'] == ident), None)
        if not require_verified and model is not None and model['availability']=='unverified_route':
            model = dict(model,selectable=True)
        if model is None or not model.get('selectable'):
            raise ValueError(catalog.get('directory_error') or '当前目录未提供这个模型，或独立路由尚未核验；选择已保留，未发送')
        _, config, _ = self._model_configuration()
        result = copy.deepcopy(model)
        supported = result['supported_reasoning_efforts']
        configured_effort = config.get('model_reasoning_effort')
        answer = configured_effort if configured_effort in supported else result['default_reasoning_effort']
        route = next((e for e in _EFFORTS if e in supported), None)
        result.update(reasoning_effort=answer, routing_effort=route,
            selection_fingerprint=hashlib.sha256((catalog['scope_fingerprint']+self._configuration_fingerprint(model)).encode()).hexdigest())
        return result

    def require_model_selection(self, selected):
        current = self.resolve_model(selected.get('id'))
        if selected.get('selection_fingerprint') != current['selection_fingerprint']:
            raise ValueError('模型目录、账户或路由已变化，请保留问题并重新选择或预览')
        return current
