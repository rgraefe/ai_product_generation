import tempfile
import unittest
from pathlib import Path

from productgen.storage import Store
from server import Application
from test_productgen import product


class InstructionPresetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / 'data'
        self.app = Application(self.directory, Path(self.temp.name) / 'exports')
        self.store = self.app.store

    def tearDown(self):
        self.temp.cleanup()

    def test_presets_persist_and_work_across_categories_without_changing_drafts(self):
        text = 'Keine Pflegehinweise erfinden.\nGeschenkverpackung nicht erwähnen.'
        saved = self.store.save_instruction_preset({'label': ' Geschenkartikel ', 'instructions': text})
        self.assertEqual(Store(self.directory).instruction_presets(), [saved])
        scarf = self.app.create({'product': product(instructions=text)})
        necklace = self.app.create({'product': product(category='kette', sku='TEST-002', instructions=text)})
        updated = self.store.save_instruction_preset({**saved, 'label': 'Geschenke', 'instructions': 'Neue Anweisungen'})
        self.assertEqual(updated['revision'], 2)
        for draft in (scarf, necklace):
            self.assertEqual(self.store.get(draft['id'])['input']['instructions'], text)

    def test_duplicate_labels_do_not_overwrite_saved_instructions(self):
        saved = self.store.save_instruction_preset({'label': 'Floral', 'instructions': 'Describe flowers.'})
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.store.save_instruction_preset({'label': ' floral ', 'instructions': 'Different text'})
        self.assertEqual(self.store.instruction_presets(), [saved])

    def test_stale_update_is_rejected(self):
        saved = self.store.save_instruction_preset({'label': 'Gifts', 'instructions': 'Original'})
        self.store.save_instruction_preset({**saved, 'instructions': 'Updated'})
        with self.assertRaisesRegex(ValueError, 'another window'):
            self.store.save_instruction_preset({**saved, 'instructions': 'Stale update'})

    def test_blank_and_invalid_input_is_rejected(self):
        for data in ({'label':' ', 'instructions':'Text'}, {'label':'Label','instructions':' '}, {'label':None,'instructions':'Text'}, {'label':'x'*121,'instructions':'Text'}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.store.save_instruction_preset(data)


if __name__ == '__main__':
    unittest.main()
