from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from wiki_checks import PageIndex, check_anchor, filter_matches, markdown_targets, strip_fences, active, navigation_text

class WikiChecksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        for name in ['wiki/methods/nested/A.md', 'wiki/papers/A.md', 'wiki/index.md']:
            p = self.root/name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('# Heading\ntext ^block\n')
        self.idx = PageIndex(self.root)
        self.origin = self.root/'wiki/index.md'

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_basename_fallback_for_wrong_directory(self):
        with self.assertRaises(ValueError):
            self.idx.resolve('absent/A', self.origin)

    def test_ambiguous_basename_fails_exact_path_resolves(self):
        with self.assertRaises(ValueError):
            self.idx.resolve('A', self.origin)
        self.assertEqual(self.idx.resolve('wiki/methods/nested/A', self.origin)[0], self.root/'wiki/methods/nested/A.md')

    def test_recursive_active_and_block_anchor(self):
        p = self.root/'wiki/methods/nested/A.md'
        self.assertTrue(active(p, self.root))
        check_anchor(p, '^block')
        with self.assertRaises(ValueError):
            check_anchor(p, 'absent')

    def test_balanced_parentheses_and_angle_links(self):
        text = '[a](file:///tmp/a(b).md) ![img](</tmp/img (x).png>)'
        self.assertEqual(list(markdown_targets(text)), ['file:///tmp/a(b).md', '/tmp/img (x).png'])

    def test_base_filters_actually_check_state(self):
        p = self.root/'wiki/opportunities/card.md'
        self.assertFalse(filter_matches({'and':['file.inFolder("wiki/opportunities")','note.state == "data_ready"']},p,{'state':'verifying'},self.root))
        with self.assertRaises(ValueError):
            filter_matches('unknown.function()',p,{},self.root)

    def test_fences_keep_line_numbers(self):
        text = 'a\n````md\n[[bad]]\n```\n[[also-code]]\n````\nz'
        stripped = strip_fences(text)
        self.assertEqual(len(stripped.splitlines()), len(text.splitlines()))
        self.assertNotIn('bad', stripped)

    def test_source_quote_links_are_not_navigation(self):
        quoted = '> "[original](missing-original.md) and [[quoted]]" — raw/source.md\n'
        text = quoted + '[real](real.md)\n> "[malformed](bad.md)" raw/source.md\n'
        self.assertEqual(list(markdown_targets(navigation_text(text))), ['real.md', 'bad.md'])

if __name__ == '__main__':
    unittest.main()
