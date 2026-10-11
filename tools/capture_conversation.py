#!/usr/bin/env python3
"""Explicit local capture client. Never scans or monitors any Codex conversation.

Examples:
  python3 tools/capture_conversation.py save --file capture.json
  cat capture.json | python3 tools/capture_conversation.py save
  python3 tools/capture_conversation.py status REQUEST_ID
  python3 tools/capture_conversation.py recent --limit 10
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('本机接口不允许重定向')


def local_url(value):
    parsed=urlsplit(value)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        raise ValueError('只允许 http://127.0.0.1:端口 或 http://localhost:端口')
    if not parsed.port or not 1<=parsed.port<=65535: raise ValueError('必须指定本机端口')
    return value.rstrip('/')


def call(opener, base, path, payload=None, token=None):
    body=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
    if body is not None and len(body)>=120000: raise ValueError('请求超过本机接口大小限制')
    headers={'Accept':'application/json'}
    if body is not None: headers.update({'Content-Type':'application/json','X-Desk-Token':token})
    request=Request(base+path,data=body,headers=headers)
    try:
        with opener.open(request,timeout=30) as response:
            if response.headers.get_content_type()!='application/json': raise ValueError('本机接口未返回 JSON')
            raw=response.read(2_000_001)
            if len(raw)>2_000_000: raise ValueError('本机接口响应过大')
            return json.loads(raw)
    except HTTPError as exc:
        message='本机接口错误 '+str(exc.code)
        try:
            value=json.loads(exc.read(120000))
            if isinstance(value.get('error'),str): message=value['error']
        except (ValueError,AttributeError): pass
        raise ValueError(message) from None


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--url',default='http://127.0.0.1:60128')
    commands=parser.add_subparsers(dest='command',required=True)
    save=commands.add_parser('save');save.add_argument('--file',help='JSON 文件；省略或 - 从标准输入读取')
    status=commands.add_parser('status');status.add_argument('request_id')
    recent=commands.add_parser('recent');recent.add_argument('--limit',type=int,default=20)
    args=parser.parse_args(argv)
    try:
        base=local_url(args.url);opener=build_opener(ProxyHandler({}),NoRedirect())
        identity=call(opener,base,'/api/identity')
        if identity.get('application')!='growth-desk': raise ValueError('目标端口不是成长工作台')
        if args.command=='save':
            if args.file and args.file!='-':
                with Path(args.file).expanduser().open('rb') as stream: raw=stream.read(120001)
            else: raw=sys.stdin.buffer.read(120001)
            if len(raw)>120000: raise ValueError('JSON 文件过大')
            payload=json.loads(raw)
            if not isinstance(payload,dict): raise ValueError('JSON 必须是对象')
            # Token is used only in this process; never print the state response.
            state=call(opener,base,'/api/state');token=state.get('token')
            if not isinstance(token,str) or not token: raise ValueError('本机令牌不可用')
            result=call(opener,base,'/api/capture',payload,token)
        elif args.command=='status': result=call(opener,base,'/api/capture?'+urlencode({'request_id':args.request_id}))
        else:
            if not 1<=args.limit<=100: raise ValueError('数量应为 1 至 100')
            result=call(opener,base,'/api/capture?'+urlencode({'limit':args.limit}))
        # Defensive stripping: authentication data never belongs in a receipt.
        if isinstance(result,dict): result.pop('token',None)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if isinstance(result,dict) and result.get('status')=='partial' else 0
    except (ValueError,OSError,URLError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__': raise SystemExit(main())
