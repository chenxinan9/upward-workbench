"""Synthetic verified model for unit tests; no real model connection is used."""
import json
from app import Desk as ProductionDesk
class Desk(ProductionDesk):
 def __init__(self,path):
  super().__init__(path)
  self.model_home=self.store.path/'fixture-model-home';self.model_home.mkdir(exist_ok=True)
  (self.model_home/'config.toml').write_text('model = "gpt-fixture"\nmodel_reasoning_effort = "high"\nmodel_provider = "fixture"\n')
  self.verify_model('gpt-fixture','gpt-fixture')
