#!/usr/bin/env python3
"""Recompute distributable metrics without opening personal configuration."""
import json,collections
from pathlib import Path
root=Path(__file__).resolve().parents[1]
def metrics():
 advisors=json.loads((root/'public-advisors.json').read_text());workflows=json.loads((root/'methods.json').read_text());quotes=json.loads((root/'quotes.json').read_text())
 bundle=json.loads((root/'advisors-bundle/manifest.json').read_text())['entries'];methods=json.loads((root/'advisors-bundle/methods.json').read_text())
 knowledge=json.loads((root/'knowledge-bundle/manifest.json').read_text()) if (root/'knowledge-bundle/manifest.json').exists() else {'collections':[],'files':[]}
 refs=sum(len([f for f in json.loads((root/'advisors-bundle'/e['path']/'provenance.json').read_text())['files'] if f['path'].startswith(('references/','examples/'))]) for e in bundle)
 return {'bundled_advisor_reference_files':refs,'public_knowledge_collections':len(knowledge['collections']),'bundled_public_knowledge_markdown_files':sum(f['path'].endswith('.md') for f in knowledge['files']),'public_knowledge_chapters':sum(f['path'].startswith('life-guide/book/') and f['path'].endswith('.md') for f in knowledge['files']),'public_knowledge_support_documents':sum(f['path'].startswith('life-guide/docs/') and f['path'].endswith('.md') for f in knowledge['files']),'public_source_cards':sum(f['path'].startswith('advisor-reading/S') and f['path'].endswith('.md') for f in knowledge['files']),'advisor_source_entries':len(advisors),'source_categories':len({a['category'] for a in advisors}),'registered_workflows':len(workflows),'sourced_reminders':len(quotes),'bundled_adapted_skills':len(bundle),'registered_deduplicated_methods':len(methods),'methods_scope':'reviewed retained subset; not all methods in all original texts','bundled_third_party_full_skills':0,'bundle_license_distribution':dict(collections.Counter(a['license'] for a in bundle)),'local_installed_count':'requires local connection; not measured by public package','private_knowledge_documents':'requires local connection; not measured by public package','outcomes':'not measured'}
if __name__=='__main__':print(json.dumps(metrics(),ensure_ascii=False,indent=2))
