import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import os
import shutil
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from source_registry import SourceSpec, RegistryError, load_registry, lookup, publish_batch, validate_evidence, sha256
from source_registry import VALIDATION_CONTEXT_ENV



class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        self.source = self.vault / "inbox/source.md"
        self.source.parent.mkdir()
        self.source.write_text("First exact sentence.\nSecond line.\n")

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, **kwargs):
        source = SourceSpec("test", self.source, "fixture", **kwargs)
        result = publish_batch([source], vault=self.vault, write=True)
        record = next(iter(result["manifest"]["sources"].values()))["versions"][-1]
        return result, record

    def test_idempotent_and_versioned(self):
        first, old = self.publish()
        manifest = self.vault / "sources/manifest.json"
        before, mtime = manifest.read_bytes(), manifest.stat().st_mtime_ns
        again, _ = self.publish()
        self.assertFalse(again["changed"])
        self.assertEqual(again["new_versions"], 0)
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(manifest.stat().st_mtime_ns, mtime)
        self.source.write_text("Changed sentence.\n")
        changed, new = self.publish()
        self.assertEqual(changed["new_versions"], 1)
        self.assertNotEqual(old["version"], new["version"])
        self.assertEqual((self.vault / old["snapshot_path"]).read_text(), "First exact sentence.\nSecond line.\n")
        self.assertEqual(validate_evidence(old["snapshot_path"], vault=self.vault)["status"], "stale")
        self.assertTrue(validate_evidence(new["snapshot_path"], "Changed sentence.", vault=self.vault)["ok"])

    def test_missing_required_batch_publishes_nothing(self):
        with self.assertRaises(FileNotFoundError):
            publish_batch([SourceSpec("good", self.source, "fixture"), SourceSpec("missing", self.vault / "missing", "fixture")], vault=self.vault, write=True)
        self.assertFalse((self.vault / "sources").exists())
        self.assertFalse((self.vault / ".source-snapshots").exists())

    def test_pinned_alias_cannot_change(self):
        _, old = self.publish(aliases=["inbox/source.md"])
        self.source.write_text("Tampered")
        self.assertEqual(validate_evidence("inbox/source.md", vault=self.vault)["status"], "hash_mismatch")
        with self.assertRaises(RegistryError):
            self.publish(aliases=["inbox/source.md"])
        self.assertEqual(lookup("inbox/source.md", vault=self.vault)["version"], old["version"])

    def test_only_registered_paths(self):
        _, old = self.publish()
        self.assertIsNone(lookup(self.source, vault=self.vault))
        self.assertIsNone(lookup("inbox/../" + old["snapshot_path"], vault=self.vault))
        alias = self.vault / "unregistered-link.md"
        alias.symlink_to(self.vault / old["snapshot_path"])
        self.assertIsNone(lookup(alias, vault=self.vault))
        self.assertEqual(validate_evidence(old["snapshot_path"], source_id="wrong", vault=self.vault)["status"], "version_mismatch")
        self.assertEqual(validate_evidence(old["snapshot_path"], version="wrong", vault=self.vault)["status"], "version_mismatch")

    def test_locator_is_directional(self):
        payload = {"rows": [{"record_id": "round1:1", "before": "Old wording", "after": "Adopted wording", "adoption_status": "verified_in_final"}]}
        self.source.write_text(json.dumps(payload))
        _, old = self.publish(suffix=".json")
        path = old["snapshot_path"]
        loc = {"record_id": "round1:1", "field": "after", "adoption_status": "verified_in_final"}
        self.assertTrue(validate_evidence(path, "Adopted wording", locator=loc, vault=self.vault)["ok"])
        self.assertFalse(validate_evidence(path, "Old wording", locator=loc, vault=self.vault)["ok"])
        loc["adoption_status"] = "retained_in_final"
        self.assertEqual(validate_evidence(path, "Adopted wording", locator=loc, vault=self.vault)["status"], "invalid_locator")
        self.assertEqual(validate_evidence(path, locator="invented", vault=self.vault)["status"], "invalid_locator")
        self.assertTrue(validate_evidence(path, "Old wording", locator="json:/rows/0/before", vault=self.vault)["ok"])

    def test_missing_and_tampered_snapshot(self):
        _, old = self.publish()
        snapshot = self.vault / old["snapshot_path"]
        snapshot.chmod(0o644)
        snapshot.write_text("Tampered")
        self.assertEqual(validate_evidence(old["snapshot_path"], vault=self.vault)["status"], "hash_mismatch")
        with self.assertRaises(RegistryError):
            self.publish()
        snapshot.unlink()
        self.assertEqual(validate_evidence(old["snapshot_path"], vault=self.vault)["status"], "missing")

    def test_extraction_source_race_rejected(self):
        old_hash = sha256(self.source.read_bytes())
        self.source.write_text("changed after extraction")
        with self.assertRaises(RegistryError):
            self.publish(content=b"old extraction", expected_hashes={str(self.source): old_hash})
        self.assertFalse((self.vault / "sources/manifest.json").exists())

    def test_absolute_alias_and_snapshot_boundaries(self):
        absolute = str(self.source.resolve())
        _, old = self.publish(aliases=[absolute])
        self.assertTrue(validate_evidence(absolute, vault=self.vault)['ok'])
        snapshot = self.vault / old['snapshot_path']
        self.source.unlink()
        self.source.symlink_to(snapshot)
        self.assertFalse(validate_evidence(absolute, vault=self.vault)['ok'])
        # A poisoned manifest cannot point snapshots outside their owned directory.
        manifest = load_registry(self.vault)
        record = next(iter(manifest['sources'].values()))['versions'][0]
        record['snapshot_path'] = '/etc/hosts'
        self.assertFalse(validate_evidence('/etc/hosts', vault=self.vault, manifest=manifest)['ok'])
        record['snapshot_path'] = 'inbox/source.md'
        self.assertFalse(validate_evidence(absolute, vault=self.vault, manifest=manifest)['ok'])

    def test_registered_snapshot_replaced_by_symlink(self):
        _, old = self.publish()
        snapshot = self.vault / old['snapshot_path']
        snapshot.unlink()
        snapshot.symlink_to(self.source)
        self.assertFalse(validate_evidence(old['snapshot_path'], vault=self.vault)['ok'])

    def test_candidate_context_maps_origin_and_absolute_alias_without_manifest_edits(self):
        _, old = self.publish(aliases=[str(self.source.resolve())])
        original = self.vault.resolve()
        with tempfile.TemporaryDirectory() as name:
            exported = Path(name).resolve() / 'tree'
            shutil.copytree(original, exported)
            before = (exported / 'sources/manifest.json').read_bytes()
            context = json.dumps({'original_root': str(original), 'export_root': str(exported)})
            with patch.dict(os.environ, {VALIDATION_CONTEXT_ENV: context}):
                result = validate_evidence(str(self.source.resolve()), 'First exact sentence.', vault=exported)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['record']['source_path'], str(self.source.resolve()))
                (exported / 'inbox/source.md').write_text('Changed only inside candidate')
                self.assertEqual(validate_evidence(old['snapshot_path'], vault=exported)['status'], 'stale')
                self.assertEqual(validate_evidence(str(self.source.resolve()), vault=exported)['status'], 'hash_mismatch')
                (exported / 'inbox/source.md').unlink()
                self.assertEqual(validate_evidence(old['snapshot_path'], vault=exported)['status'], 'stale')
                self.assertEqual(validate_evidence(str(self.source.resolve()), vault=exported)['status'], 'missing')
            self.assertEqual((exported / 'sources/manifest.json').read_bytes(), before)

    def test_candidate_context_does_not_use_changed_worktree_origin(self):
        _, old = self.publish()
        original = self.vault.resolve()
        with tempfile.TemporaryDirectory() as name:
            exported = Path(name).resolve() / 'tree'
            shutil.copytree(original, exported)
            self.source.write_text('Unowned original worktree modification')
            with patch.dict(os.environ, {VALIDATION_CONTEXT_ENV: json.dumps({'original_root': str(original), 'export_root': str(exported)})}):
                self.assertTrue(validate_evidence(old['snapshot_path'], vault=exported)['ok'])
                # A candidate parent symlink must never fall back to the original tree.
                shutil.rmtree(exported / 'inbox')
                (exported / 'inbox').symlink_to(original / 'inbox')
                self.assertEqual(validate_evidence(old['snapshot_path'], vault=exported)['status'], 'stale')

    def test_candidate_context_external_prefix_sibling_remains_real_origin(self):
        original = self.vault.resolve()
        with tempfile.TemporaryDirectory() as name:
            area = Path(name).resolve()
            sibling = original.with_name(original.name + '-sibling.md')
            sibling.write_text('External real evidence')
            self.addCleanup(sibling.unlink, missing_ok=True)
            result = publish_batch([SourceSpec('external', sibling, 'fixture')], vault=original, write=True)
            record = next(iter(result['manifest']['sources'].values()))['versions'][0]
            exported = area / 'tree'
            shutil.copytree(original, exported)
            with patch.dict(os.environ, {VALIDATION_CONTEXT_ENV: json.dumps({'original_root': str(original), 'export_root': str(exported)})}):
                self.assertTrue(validate_evidence(record['snapshot_path'], vault=exported)['ok'])
                sibling.write_text('Changed outside candidate')
                self.assertEqual(validate_evidence(record['snapshot_path'], vault=exported)['status'], 'stale')

    def test_candidate_context_rejects_mismatched_roots(self):
        _, old = self.publish()
        original = self.vault.resolve()
        for context in ({'original_root': str(original), 'export_root': str(original)},
                        {'original_root': str(original), 'export_root': '/wrong-root'},
                        {'original_root': '../relative', 'export_root': str(original)}):
            with patch.dict(os.environ, {VALIDATION_CONTEXT_ENV: json.dumps(context)}):
                self.assertFalse(validate_evidence(old['snapshot_path'], vault=original)['ok'])


if __name__ == "__main__":
    unittest.main()
