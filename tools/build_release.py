#!/usr/bin/env python3
"""Explicit allowlist release builder. No parent-directory collection or publishing."""
import argparse,ast,hashlib,json,re,zipfile,sys
from pathlib import Path
try:
 from .metrics import metrics
 from .check_public import check
except ImportError:
 from metrics import metrics
 from check_public import check
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from bundle_resources import advisor_resources, knowledge_resources, relative_path, read_resource
FILES=['docs/codex-native-capabilities.md','attachments.py','codex_native.py','README.md','LICENSE','NOTICE','.gitignore','app.py','start.command','methods.json','quotes.json','public-advisors.json','web/index.html','web/app.js','web/style.css','tools/backup.py','tools/connect_local.py','tools/metrics.py','tools/build_release.py','tests/test_core.py','docs/product.md','docs/acceptance-criteria.md','docs/acceptance-report.md','docs/audit-plan.md','docs/audit-findings.md','docs/research.md','docs/distribution-policy.md','docs/integration.md','docs/process.md','docs/validation.md']
FILES += ['growth.py','tools/plan_index.py','tests/test_growth.py','tests/test_frontend_steps.cjs','desktop/README.md','desktop/install.py','desktop/start_service.py','desktop/GrowthDesk.swift','desktop/icon.swift','docs/v2-workflow.md','docs/v2-validation.md','docs/v2-independent-acceptance.md','docs/advisor-bundle-audit.md','advisors-bundle/README.md','advisors-bundle/manifest.json','advisors-bundle/methods.json','advisors-bundle/install.py','advisors-bundle/excluded.json','advisors-bundle/bundle-files.json']
FILES += ['desktop/assets/upward-icon.png','desktop/assets/study-scene.png','desktop/assets/README.md','docs/page-responsibilities.md','docs/release-v0.4.7-rc1.md']
FILES += ['CONTRIBUTING.md','SECURITY.md','ROADMAP.md','docs/daily-use.md','docs/getting-started.md','.github/workflows/check.yml','tools/check_public.py','tools/setup_knowledge.py','tests/test_advisor.py','tests/test_knowledge_setup.py','skills/upward-advisor/SKILL.md','skills/upward-advisor/scripts/advisor.py','examples/README.md','examples/growth-plan.sample.json','examples/init_demo.py','examples/sources/profile.md','examples/sources/project.md','examples/sources/method.md']
FILES += ['capture.py','conversation.py','advisor_council.py','tools/capture_conversation.py','tests/test_capture.py','tests/test_conversation.py','docs/conversation-capture.md','tests/test_advisor_council.py','tests/test_council_pipeline.py','docs/advisor-council.md']
FILES += ['tests/test_experience.py','tests/test_existing_updates.py','tests/test_chat_experience.cjs','docs/daily-experience.md']
FILES += ['docs/models-and-streaming.md','stream_transport.py','model_access.py','model_check.py','tests/desk_fixture.py','tests/test_stream_transport.py','tests/test_model_access.py','tests/test_model_frontend.cjs','tests/test_model_check.py','docs/phase-local.md','phase.py','local_tools.py','web/experience.js','web/experience.css','tests/test_local_tools.py','tests/test_phase.py','tests/test_phase_frontend.cjs']
FILES += ['docs/current-workspace.md','docs/knowledge-library.md','docs/release-v0.4.4-rc1.md','docs/release-v0.3.0-rc2.md','docs/advisors.md','docs/reference.md','docs/release-v0.3.0-rc1.md']
# The network installer and its checksum test remain repository-only to avoid a self-referential archive hash.
FILES += ['creator_evidence.py','tests/test_creator_evidence.py','tests/test_cross_workbench_acceptance.py','tests/test_advisor_update.py','tests/test_workspace_sessions.py']
FILES += ['workspace_artifacts.py','tests/test_workspace_artifacts.py','tests/test_workspace_output.py','tests/test_desktop_navigation.py']
FILES += ['provider_access.py','tests/test_provider_access.py','docs/deepseek-and-codex.md','docs/release-v0.4.8-rc1.md','docs/licensing.md','LICENSES/legacy-MIT.txt']
FILES += ['shared_knowledge.py','tests/test_shared_knowledge.py','tests/test_backend_retention.py','tests/test_attachments.py']
FILES += ['growth_plan.py','growth_workspace.py','web/growth-workspace.js','web/growth-workspace.css','tests/test_growth_plan.py','tests/test_growth_context_acceptance.py','tests/test_growth_workspace.py','tests/test_growth_workspace.cjs',
          'workspace_api.py','vault.py','codex_workspace.py','web/workspace.html','web/workspace.js','web/workspace.css',
          'tools/migrate_workspace.py','tests/test_vault.py','tests/test_workspace_api.py','tests/test_capability_audit.py','tests/test_codex_workspace.py',
          'tests/test_workspace_migration.py','tests/test_workspace_frontend.cjs','tests/test_workspace_install.py','tests/test_private_backup.py','docs/workspace-migration.md']


def resource_files():
 resources={}
 entries=json.loads(read_resource(ROOT/'advisors-bundle','manifest.json'))['entries']
 seen=set()
 for entry in entries:
  name=relative_path(entry['path']).as_posix()
  if name!=entry['name'] or '/' in name or name in seen:raise ValueError('顾问清单路径异常')
  seen.add(name)
  content=advisor_resources(ROOT/'advisors-bundle'/name)
  resources.update(('advisors-bundle/'+name+'/'+rel,data) for rel,data in content.items())
 _,knowledge=knowledge_resources(ROOT/'knowledge-bundle')
 resources.update(('knowledge-bundle/'+rel,data) for rel,data in knowledge.items())
 return resources


def release_files():
 paths=list(FILES)+['bundle_resources.py','tests/test_bundle_resources.py']+list(resource_files())
 if len(paths)!=len(set(paths)):raise ValueError('白名单条目重复')
 return paths


def validate_dependencies(paths):
 allowed=set(paths)
 for rel in paths:
  if not rel.endswith('.py'):continue
  tree=ast.parse((ROOT/rel).read_text(),filename=rel)
  for node in ast.walk(tree):
   names=[a.name.split('.')[0] for a in node.names] if isinstance(node,ast.Import) else [node.module.split('.')[0]] if isinstance(node,ast.ImportFrom) and node.module else []
   for name in names:
    local=name+'.py'
    if (ROOT/local).is_file() and local not in allowed:raise ValueError('公开包缺少本地依赖：'+rel+' -> '+local)
 for rel in ['web/index.html','web/workspace.html']:
  for target in re.findall(r'(?:src|href)=["\'](/[^"\']+\.(?:js|css))["\']',(ROOT/rel).read_text()):
   if 'web'+target not in allowed:raise ValueError('公开包缺少网页依赖：'+target)


def build(output):
 out=Path(output).expanduser().resolve()
 if out==ROOT or ROOT in out.parents:raise ValueError('候选包输出必须放在源码目录之外')
 if out.exists():raise ValueError('拒绝覆盖现有候选包；使用新版本文件名')
 paths=release_files()
 if any('real_workspace_probe' in p or 'schema-evidence' in p for p in paths):raise ValueError('真实探针与内部协议证据不得进入公开包')
 scan=check(ROOT,paths)
 if not scan['passed']:raise ValueError(json.dumps(scan,ensure_ascii=False,indent=2))
 validate_dependencies(paths)
 reviewed=resource_files()
 if not set(reviewed)<=set(paths):raise ValueError('资料清单在构建期间发生变化')
 content={}
 for rel in paths:
  f=ROOT/rel
  if f.is_symlink() or ROOT not in f.resolve().parents:raise ValueError('白名单路径异常：'+rel)
  data=reviewed[rel] if rel in reviewed else f.read_bytes()
  if rel in ('desktop/assets/upward-icon.png','desktop/assets/study-scene.png'):
   if not data.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('图标不是PNG')
   content[rel]=data;continue
  text=data.decode('utf-8')
  if re.search(r'/(?:Users|home)/[A-Za-z0-9_.-]+/',text):raise ValueError('疑似真实用户绝对路径：'+rel)
  if re.search(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|sk-[A-Za-z0-9]{30,}',text):raise ValueError('疑似凭据：'+rel)
  content[rel]=data
 manifest={'version':'0.4.8-rc1','edition':'source-available-growth-workspace-preview','scope':'personal-use licensed new original changes, retained legacy and third-party licenses; no private user data','metrics':metrics(),'files':[{'path':p,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()} for p,b in sorted(content.items())]}
 content['release-manifest.json']=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
 out.parent.mkdir(parents=True,exist_ok=True)
 with zipfile.ZipFile(out,'x',zipfile.ZIP_DEFLATED) as z:
  for rel,data in sorted(content.items()):
   i=zipfile.ZipInfo('growth-desk/'+rel,date_time=(2026,10,11,0,0,0));i.compress_type=zipfile.ZIP_DEFLATED;i.external_attr=(0o100755 if rel in ['start.command','desktop/install.py','advisors-bundle/install.py'] else 0o100644)<<16;z.writestr(i,data)
 return {'archive':str(out),'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'files':len(content),'bytes':out.stat().st_size,'metrics':metrics()}


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);a=p.parse_args()
 try:print(json.dumps(build(a.output),ensure_ascii=False,indent=2))
 except (ValueError,OSError) as error:p.exit(1,str(error)+'\n')

if __name__=='__main__':main()
