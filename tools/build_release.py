#!/usr/bin/env python3
"""Explicit allowlist release builder. No parent-directory collection or publishing."""
import argparse,hashlib,json,re,zipfile
from pathlib import Path
from metrics import metrics
from check_public import check
ROOT=Path(__file__).resolve().parents[1]
FILES=['README.md','LICENSE','NOTICE','.gitignore','app.py','start.command','methods.json','quotes.json','public-advisors.json','web/index.html','web/app.js','web/style.css','tools/backup.py','tools/connect_local.py','tools/metrics.py','tools/build_release.py','tests/test_core.py','docs/product.md','docs/acceptance-criteria.md','docs/acceptance-report.md','docs/audit-plan.md','docs/audit-findings.md','docs/research.md','docs/distribution-policy.md','docs/integration.md','docs/process.md','docs/validation.md']
FILES += ['growth.py','tools/plan_index.py','tests/test_growth.py','tests/test_frontend_steps.cjs','desktop/README.md','desktop/install.py','desktop/start_service.py','desktop/GrowthDesk.swift','desktop/icon.swift','docs/v2-workflow.md','docs/v2-validation.md','docs/v2-independent-acceptance.md','docs/advisor-bundle-audit.md','advisors-bundle/README.md','advisors-bundle/manifest.json','advisors-bundle/methods.json','advisors-bundle/install.py','advisors-bundle/excluded.json','advisors-bundle/bundle-files.json']
for e in json.loads((ROOT/'advisors-bundle/manifest.json').read_text())['entries']:
 for filename in ['SKILL.md','LICENSE','provenance.json']:FILES.append('advisors-bundle/'+e['path']+'/'+filename)
FILES += ['desktop/assets/upward-icon.png','desktop/assets/README.md','docs/page-responsibilities.md']
FILES += ['CONTRIBUTING.md','SECURITY.md','ROADMAP.md','docs/daily-use.md','docs/getting-started.md','.github/workflows/check.yml','tools/check_public.py','tools/setup_knowledge.py','tests/test_advisor.py','tests/test_knowledge_setup.py','skills/upward-advisor/SKILL.md','skills/upward-advisor/scripts/advisor.py','examples/README.md','examples/growth-plan.sample.json','examples/init_demo.py','examples/sources/profile.md','examples/sources/project.md','examples/sources/method.md']
FILES += ['install.sh', 'tests/test_quickstart.py', 'docs/reference.md', 'docs/advisors.md']
if len(FILES)!=len(set(FILES)):raise SystemExit('白名单条目重复')
p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();out=Path(a.output).expanduser().resolve()
if out==ROOT or ROOT in out.parents:raise SystemExit('候选包输出必须放在源码目录之外')
if out.exists():raise SystemExit('拒绝覆盖现有候选包；使用新版本文件名')
scan=check(ROOT,FILES)
if not scan['passed']:raise SystemExit(json.dumps(scan,ensure_ascii=False,indent=2))
content={}
for rel in FILES:
 f=ROOT/rel
 if f.is_symlink() or ROOT not in f.resolve().parents:raise SystemExit('白名单路径异常：'+rel)
 data=f.read_bytes()
 if rel=='desktop/assets/upward-icon.png':
  if not data.startswith(b'\x89PNG\r\n\x1a\n'):raise SystemExit('图标不是PNG')
  content[rel]=data;continue
 text=data.decode('utf-8')
 if re.search(r'/(?:Users|home)/[A-Za-z0-9_.-]+/',text):raise SystemExit('疑似真实用户绝对路径：'+rel)
 if re.search(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|sk-[A-Za-z0-9]{30,}',text):raise SystemExit('疑似凭据：'+rel)
 content[rel]=data
manifest={'version':'0.2.1','scope':'source candidate; not authorization to publish','metrics':metrics(),'files':[{'path':p,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()} for p,b in sorted(content.items())]}
content['release-manifest.json']=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
out.parent.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(out,'x',zipfile.ZIP_DEFLATED) as z:
 for rel,data in sorted(content.items()):
  i=zipfile.ZipInfo('growth-desk/'+rel,date_time=(2026,10,6,0,0,0));i.compress_type=zipfile.ZIP_DEFLATED;i.external_attr=(0o100755 if rel in ['start.command','install.sh','desktop/install.py','advisors-bundle/install.py'] else 0o100644)<<16;z.writestr(i,data)
print(json.dumps({'archive':str(out),'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'files':len(content),'bytes':out.stat().st_size,'metrics':metrics()},ensure_ascii=False,indent=2))
