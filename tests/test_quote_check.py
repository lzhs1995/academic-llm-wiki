import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import yaml

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
from source_registry import SourceSpec, publish_batch
spec = importlib.util.spec_from_file_location('quote_check', TOOLS / 'quote-check.py')
qc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qc)


class QuoteCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.page = self.root / 'wiki/research/test.md'
        self.page.parent.mkdir(parents=True)
        self.raw = self.root / 'raw/source.md'
        self.raw.parent.mkdir()
        self.raw.write_text('Exact statement.\nSecond statement.\n')
        self.comparison = self.root / 'original.json'
        self.comparison.write_text(json.dumps({'rows': [{'record_id': 'r1', 'before': 'Old sentence', 'after': 'New sentence', 'adoption_status': 'verified_in_final'}]}))
        result = publish_batch([SourceSpec('raw', self.raw, 'legacy_observed', aliases=['raw/source.md']),
                                SourceSpec('comparison', self.comparison, 'adoption_verification', suffix='.json')], vault=self.root, write=True)
        self.manifest = result['manifest']
        self.records = {e['logical_id']: e['versions'][0] for e in self.manifest['sources'].values()}

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, which='raw', **kwargs):
        record = self.records[which]
        return {'path': 'raw/source.md' if which == 'raw' else record['snapshot_path'], 'source_id': record['source_id'],
                'version': record['version'], 'locator': 'lines:1-1', 'quote': 'Exact statement.', **kwargs}

    def check(self, evidence=None, body=''):
        fm = {'type': 'research'}
        if evidence is not None:
            fm['evidence'] = evidence
        self.page.write_text('---\n' + yaml.safe_dump(fm, allow_unicode=True) + '---\n' + body)
        return qc.validate_page(self.page, self.root, self.manifest)[0]

    def test_valid_legacy_quote_and_pinned_metadata(self):
        self.assertEqual(self.check([self.item()], '> "Exact statement." — raw/source.md\n'), [])

    def test_no_evidence_and_unbound_quote_fail(self):
        self.assertTrue(self.check(body='unsupported factual claim'))
        self.assertTrue(self.check(body='> "Exact statement." — raw/source.md\n'))
        self.assertTrue(self.check([self.item(quote='Second statement.', locator='lines:2-2')], '> "Exact statement." — raw/source.md\n'))

    def test_malformed_quote_and_yaml_fail(self):
        self.assertTrue(self.check([self.item()], '> "Exact statement." raw/source.md\n'))
        self.assertTrue(self.check([self.item()], '> "Exact statement." — unknown/source.md\n'))
        self.page.write_text('---\nevidence: [broken\n---\ntext')
        self.assertTrue(qc.validate_page(self.page, self.root, self.manifest)[0])

    def test_blank_quote_wrong_text_and_field_types_fail(self):
        for quote in ['', '   ', 'invented statement', 123]:
            self.assertTrue(self.check([self.item(quote=quote)]))
        for update in [{'path': []}, {'version': 3}, {'locator': ['document']}, {'direction': ['after']}]:
            self.assertTrue(self.check([self.item(**update)]))
        self.assertTrue(self.check('not a list'))
        self.assertTrue(self.check([{'path': 'raw/source.md'}]))

    def test_missing_tampered_and_wrong_version_fail(self):
        self.assertTrue(self.check([self.item(version='wrong')]))
        self.assertTrue(self.check([self.item(source_id='wrong')]))
        self.raw.write_text('Changed')
        self.assertTrue(self.check([self.item()]))
        self.raw.unlink()
        self.assertTrue(self.check([self.item()]))

    def test_outside_traversal_and_unregistered_symlink_fail(self):
        self.assertTrue(self.check([self.item(path='/etc/hosts')]))
        self.assertTrue(self.check([self.item(path='raw/../raw/source.md')]))
        (self.root / 'raw/link.md').symlink_to(self.raw)
        self.assertTrue(self.check([self.item(path='raw/link.md')]))

    def test_before_cannot_be_declared_accepted_after(self):
        loc = {'record_id': 'r1', 'field': 'before', 'adoption_status': 'verified_in_final'}
        before = self.item('comparison', locator=loc, quote='Old sentence', direction='before')
        self.assertEqual(self.check([before]), [])
        for direction in ['after', 'accepted', 'retained']:
            self.assertTrue(self.check([{**before, 'direction': direction}]))
        after = self.item('comparison', locator={**loc, 'field': 'after'}, quote='New sentence', direction='accepted')
        self.assertEqual(self.check([after]), [])
        self.assertTrue(self.check([{**after, 'quote': 'Old sentence'}]))
        self.assertTrue(self.check([{**after, 'locator': 'document'}]))
        self.assertTrue(self.check([{**after, 'locator': {**loc, 'field': 'after', 'adoption_status': 'retained_in_final'}}]))

    def test_document_identity_allowed_without_claiming_quote(self):
        item = self.item(locator='document', purpose='source_identity_only')
        item.pop('quote')
        self.assertEqual(self.check([item]), [])


if __name__ == '__main__':
    unittest.main()
