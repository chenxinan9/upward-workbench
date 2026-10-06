"""Round-trip of the public notes index and safe connection refusal."""
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import Desk
spec = importlib.util.spec_from_file_location('setup_knowledge', ROOT/'tools/setup_knowledge.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

class KnowledgeSetupTests(unittest.TestCase):
    def test_note_round_trip_and_idempotent_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)/'desk'
            module.setup(data)
            desk = Desk(data)
            note = desk.note({'body': '摄影合成练习：背光实验留证据'})
            desk.store.change(note['id'], 'note', index_generation='fixture')
            desk._index(note['id'], 'fixture')
            self.assertEqual(desk.store.get(note['id'])['index_state'], 'indexed')
            self.assertTrue(desk.search('背光'))
            before = (data/'settings.json').read_bytes()
            module.setup(data)
            self.assertEqual((data/'settings.json').read_bytes(), before)
            self.assertEqual(len(Desk(data).catalog()), 51)

    def test_existing_connection_and_symlink_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)/'desk'; data.mkdir()
            f = data/'settings.json'; raw = json.dumps({'advisor_config': str(Path(tmp)/'other.json')}); f.write_text(raw)
            with self.assertRaises(ValueError): module.setup(data)
            self.assertEqual(f.read_text(), raw)
            f.unlink(); outside = Path(tmp)/'outside'; outside.mkdir(); (data/'knowledge').symlink_to(outside)
            with self.assertRaises(ValueError): module.setup(data)
            self.assertEqual(list(outside.iterdir()), [])

if __name__ == '__main__': unittest.main()
