import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_access import ModelAccessMixin

class Models(ModelAccessMixin): pass

class ModelAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.desk=Models(); self.desk.model_home=self.root; self.desk.model_state_path=self.root/'data'
        self.write_config(); self.catalog([self.row()])
    def tearDown(self): self.tmp.cleanup()
    def write_config(self, extra=''):
        (self.root/'config.toml').write_text('model="gpt-fixture"\nmodel_reasoning_effort="xhigh"\napi_key="SECRET_SENTINEL"\n'+extra)
    def catalog(self, rows):
        (self.root/'models.json').write_text(json.dumps({'models':rows}))
        (self.root/'opencodex.config.toml').write_text('model_catalog_json="models.json"\nopenai_base_url="https://secret.invalid/route?token=TOKEN"\n')
    def row(self, ident='gpt-fixture', levels=('low','high','xhigh')):
        return {'slug':ident,'display_name':'Fixture','visibility':'list','supported_reasoning_levels':[{'effort':e} for e in levels],'base_instructions':'PRIVATE_SYSTEM_SENTINEL','secret':'SECRET_MODEL'}
    def receipt_file(self): return self.root/'data/model-verification.json'

    def test_explicit_catalog_precedes_cache_only_codex_gemini_candidates(self):
        self.catalog([self.row(),self.row('gemini-web/pro'),self.row('deepseek/no'),{**self.row('gpt-hidden'),'visibility':'hide'},{'slug':'--unsafe'}])
        (self.root/'models_cache.json').write_text(json.dumps({'models':[self.row('gpt-cached')]}))
        rows=self.desk.configured_models()
        self.assertEqual([m['id'] for m in rows],['gpt-fixture','gemini-web/pro'])
        for secret in ('SECRET','PRIVATE_SYSTEM','secret.invalid','TOKEN',str(self.root)): self.assertNotIn(secret,json.dumps(rows))
        self.assertEqual(self.desk.model_catalog()['models'],[])
        self.assertEqual(self.desk.model_catalog()['status'],'checking')

    def test_configuration_never_implies_available_and_real_receipt_required(self):
        with self.assertRaises(ValueError): self.desk.resolve_model()
        receipt=self.desk.verify_model('gpt-fixture','gpt-fixture')
        result=self.desk.model_catalog()
        self.assertEqual(result['default_model'],'gpt-fixture'); self.assertEqual(result['status'],'ready')
        self.assertTrue(result['models'][0]['verified']); self.assertEqual(result['models'][0]['verified_at'],receipt['verified_at'])
        self.assertEqual(self.receipt_file().stat().st_mode & 0o777,0o600)

    def test_mismatched_receipt_cannot_verify(self):
        with self.assertRaisesRegex(ValueError,'不一致'): self.desk.verify_model('gpt-fixture','other')
        self.assertEqual(self.desk.model_catalog()['models'],[])

    def test_resolves_effort_snapshot_and_rejects_unlisted_or_injection(self):
        self.catalog([self.row(),self.row('gemini-web/limited',('high','max'))])
        self.desk.verify_model('gpt-fixture','gpt-fixture'); self.desk.verify_model('gemini-web/limited','gemini-web/limited')
        result=self.desk.resolve_model(); self.assertEqual(result['reasoning_effort'],'xhigh'); self.assertEqual(result['routing_effort'],'low')
        limited=self.desk.resolve_model('gemini-web/limited'); self.assertEqual(limited['routing_effort'],'high'); self.assertEqual(limited['reasoning_effort'],'high')
        for ident in ('unknown','--model=x','a\n-c model=x',['gpt-fixture']):
            with self.subTest(ident=ident),self.assertRaises(ValueError): self.desk.resolve_model(ident)

    def test_dated_receipt_remains_usable_and_future_timestamp_is_rejected(self):
        self.desk.verify_model('gpt-fixture','gpt-fixture'); original=json.loads(self.receipt_file().read_text())
        for offset in (-25,1):
            state=json.loads(json.dumps(original)); state['models']['gpt-fixture']['verified_at']=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=offset)).isoformat()
            self.receipt_file().write_text(json.dumps(state)); self.assertEqual(len(self.desk.model_catalog()['models']),1 if offset < 0 else 0)

    def test_configuration_change_and_explicit_failure_remove_option(self):
        self.desk.verify_model('gpt-fixture','gpt-fixture')
        self.catalog([self.row(levels=('high',))]); self.assertEqual(self.desk.model_catalog()['models'],[])
        self.desk.verify_model('gpt-fixture','gpt-fixture'); self.assertEqual(len(self.desk.model_catalog()['models']),1)
        self.desk.invalidate_model('gpt-fixture'); self.assertEqual(self.desk.model_catalog()['models'],[])

    def test_router_binding_change_invalidates_but_credential_not_hashed_or_exposed(self):
        model=self.desk.resolve_model('gpt-fixture',require_verified=False); before=self.desk._configuration_fingerprint(model)
        p=self.root/'opencodex.config.toml'; text=p.read_text(); p.write_text(text.replace('TOKEN','NEW_SECRET'))
        self.assertEqual(before,self.desk._configuration_fingerprint(model))
        p.write_text(text.replace('secret.invalid','changed.invalid'))
        self.assertNotEqual(before,self.desk._configuration_fingerprint(model))

    def test_custom_provider_does_not_inherit_native_cache_allowlist(self):
        self.write_config('model_provider="custom"\n'); (self.root/'opencodex.config.toml').unlink()
        (self.root/'models_cache.json').write_text(json.dumps({'models':[self.row('gpt-cached')]}))
        self.assertEqual([m['id'] for m in self.desk.configured_models()],['gpt-fixture'])
        with self.assertRaises(ValueError): self.desk.resolve_model('gpt-cached',require_verified=False)

    def test_malformed_files_shapes_and_receipts_are_safe(self):
        self.catalog([None,'model',[],{'slug':'gemini-web/pro','supported_reasoning_levels':'not a list'},self.row()])
        self.assertEqual(len(self.desk.configured_models()),2)
        self.desk.verify_model('gpt-fixture','gpt-fixture'); self.receipt_file().write_text('{bad')
        self.assertEqual(self.desk.model_catalog()['models'],[])
        (self.root/'models.json').write_text('{bad'); (self.root/'config.toml').write_text('{bad')
        self.assertEqual(self.desk.configured_models(),[])

    def test_live_removal_rejects_old_selection(self):
        self.catalog([self.row(),self.row('gemini-web/temp')]); self.desk.verify_model('gemini-web/temp','gemini-web/temp')
        old=self.desk.resolve_model('gemini-web/temp'); self.catalog([self.row()])
        self.assertEqual(old['id'],'gemini-web/temp')
        with self.assertRaises(ValueError): self.desk.resolve_model(old['id'])

if __name__=='__main__':unittest.main()
