"""Native app-server wire adapters; only public progress and one-shot decisions.

Checked against local generated experimental schemas on 2026-10-12.
These adapters neither run tools themselves nor infer user approval.
"""
from pathlib import Path
import json

REQUEST_METHODS = {
    'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
    'item/tool/requestUserInput', 'tool/requestUserInput',
    'mcpServer/elicitation/request', 'item/permissions/requestApproval',
}
NATIVE_ITEMS = {'commandExecution', 'fileChange', 'mcpToolCall', 'webSearch',
                'imageView', 'imageGeneration', 'functionCallOutput'}


def native_inputs(items):
    """Host must authorize attachment ownership before calling this adapter."""
    if items is None: return []
    if not isinstance(items, list) or len(items) > 8:
        raise ValueError('图片附件数量异常')
    result = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {'type', 'path', 'detail'} or item.get('type') != 'localImage':
            raise ValueError('仅支持已授权的本地图片附件')
        path = item.get('path')
        if not isinstance(path, str) or not Path(path).is_absolute() or not Path(path).is_file():
            raise ValueError('图片附件文件不存在')
        if item.get('detail') not in (None, 'auto', 'low', 'high', 'original'):
            raise ValueError('图片精度无效')
        result.append(dict(item))
    return result


def deny_response(method):
    if method.endswith('requestUserInput'): return {'answers': {}}
    if method == 'mcpServer/elicitation/request': return {'action': 'decline', 'content': None}
    if method == 'item/permissions/requestApproval': return {'permissions': {}, 'scope': 'turn'}
    return {'decision': 'decline'}


def _subset(value, requested):
    if isinstance(value, dict):
        return isinstance(requested, dict) and all(k in requested and _subset(v, requested[k]) for k, v in value.items())
    if isinstance(value, list):
        return isinstance(requested, list) and all(v in requested for v in value)
    return type(value) is type(requested) and value == requested


def approval_response(method, params, result):
    """Validate, fail closed and never persist a grant beyond this request/turn."""
    fallback = deny_response(method)
    if not isinstance(result, dict): return fallback
    try:
        if len(json.dumps(result, allow_nan=False).encode()) > 65536: return fallback
    except (ValueError, TypeError): return fallback
    if method.endswith('requestUserInput'):
        answers = result.get('answers')
        ids = {q.get('id') for q in params.get('questions', []) if isinstance(q, dict)}
        if not isinstance(answers, dict) or set(answers) - ids: return fallback
        for answer in answers.values():
            if not isinstance(answer, dict) or set(answer) != {'answers'} or not isinstance(answer['answers'], list): return fallback
            if not all(isinstance(a, str) and len(a) <= 8000 for a in answer['answers']): return fallback
        return {'answers': answers}
    if method == 'mcpServer/elicitation/request':
        action = result.get('action')
        if action not in ('accept', 'decline', 'cancel'): return fallback
        return {'action': action, 'content': result.get('content') if action == 'accept' else None}
    if method == 'item/permissions/requestApproval':
        permissions = result.get('permissions')
        if not isinstance(permissions, dict) or not _subset(permissions, params.get('permissions', {})): return fallback
        return {'permissions': permissions, 'scope': 'turn'}
    decision = result.get('decision')
    if decision not in ('accept', 'decline', 'cancel'): return fallback
    available = params.get('availableDecisions')
    if available and decision == 'accept' and decision not in available: return fallback
    return {'decision': decision}


def public_item(item):
    """Allowlist public tool fields; never leak raw reasoning or binary results."""
    fields = {
        'commandExecution': ('command', 'cwd', 'exitCode', 'durationMs', 'aggregatedOutput'),
        'fileChange': ('changes',), 'mcpToolCall': ('server', 'tool', 'error', 'durationMs'),
        'webSearch': ('query', 'action'), 'imageView': ('path',),
        'imageGeneration': ('savedPath', 'failure'),
        'functionCallOutput': ('name', 'namespace', 'output'),
    }
    detail = {k: item[k] for k in fields.get(item.get('type'), ()) if k in item}
    # Bound individual receipts while retaining completion and identity separately.
    encoded = json.dumps(detail, ensure_ascii=False, allow_nan=False)
    return detail if len(encoded) <= 12000 else {'excerpt': encoded[:12000], 'truncated': True}
