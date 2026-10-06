#!/usr/bin/env python3
"""Recompute distributable metrics without opening personal configuration."""
import json,collections
from pathlib import Path
root=Path(__file__).resolve().parents[1]
def metrics():
 advisors=json.loads((root/'public-advisors.json').read_text());workflows=json.loads((root/'methods.json').read_text());quotes=json.loads((root/'quotes.json').read_text())
 bundle=json.loads((root/'advisors-bundle/manifest.json').read_text())['entries'];methods=json.loads((root/'advisors-bundle/methods.json').read_text())
 return {'advisor_source_entries':len(advisors),'source_categories':len({a['category'] for a in advisors}),'registered_workflows':len(workflows),'sourced_reminders':len(quotes),'bundled_adapted_skills':len(bundle),'registered_deduplicated_methods':len(methods),'methods_scope':'reviewed retained subset; not all methods in all original texts','bundled_third_party_full_skills':0,'bundle_license_distribution':dict(collections.Counter(a['license'] for a in bundle)),'local_installed_count':'requires local connection; not measured by public package','knowledge_documents':'requires local connection; not measured by public package','outcomes':'not measured'}
if __name__=='__main__':print(json.dumps(metrics(),ensure_ascii=False,indent=2))
