#!/usr/bin/env python3
"""Render a dated private planning view for an existing text index. No model or network."""
import argparse,hashlib,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--plan',required=True);p.add_argument('--output',required=True);a=p.parse_args()
f=Path(a.plan).expanduser().resolve();out=Path(a.output).expanduser().resolve();root=Path(__file__).resolve().parents[1]
if out==f or out==root or root in out.parents:raise SystemExit('索引导读必须放在源码外，且不能覆盖方案原件')
d=json.loads(f.read_text());lines=['# 个人成长方案导读（私人派生视图）','','这是从私人方案生成的检索导读，不是本人新承诺；工作台实际采用/进度看其数据库，原目标台账仍以原文件为准。','',f"生成依据日期：{d.get('generated_at','未注明')}；方案SHA-256：{hashlib.sha256(f.read_bytes()).hexdigest()}",f'方案原件：{f}','','## 当前阅读重点',d.get('current_focus_draft',{}).get('reason','未配置')]
for x in d.get('domains',[]):
 lines+=['','## '+x['title'],x.get('summary',''),x.get('priority_reason',''),'','### 已有依据']
 for fact in x.get('known_facts',[]):lines+=['- '+fact['text']+'（'+str(fact.get('date') or '日期未知')+'；'+fact.get('evidence_type','未分类')+'；来源 '+fact.get('source_id','')+'）']
 lines+=['','### 待本人确认的方案',x.get('plan_draft',{}).get('title',''),x.get('plan_draft',{}).get('why','')]
 for act in x.get('actions',[]):
  lines+=['- 候选：'+act['title']+'；原行动引用：'+act.get('canonical_action_ref','无')+'；未自动采用。','  '+ '；'.join(act.get('steps',[])),'  完成依据：'+'；'.join(act.get('completion_evidence',[]))]
 lines+=['','待核信息：'+'；'.join(x.get('unknowns',[])),'相关顾问：'+'、'.join(x.get('advisor_slugs',[]))]
lines+=['','## 来源登记']
for s in d.get('source_register',[]):lines+=['- '+s['id']+' '+s['title']+'；'+str(s.get('date') or '原文日期未注明')+'；'+s.get('path',s.get('url',s.get('external_key','')))]
out.parent.mkdir(parents=True,exist_ok=True);out.parent.chmod(0o700);out.write_text('\n'.join(lines)+'\n');out.chmod(0o600)
print(json.dumps({'written':str(out),'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'domains':len(d.get('domains',[]))},ensure_ascii=False))
