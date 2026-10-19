"""Pure metadata regressions; runnable with unittest without any application DB."""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'scripts/check_release_schema_compatibility.py'
spec = importlib.util.spec_from_file_location('schema_gate', path)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class ReleaseSchemaGateTests(unittest.TestCase):
    def evidence(self, revision='0019_explicit_test_content', column=True):
        return {'database': {'transaction_read_only': 'on', 'revisions': [revision],
                             'columns': [{'table':'videos','column':'is_test_content','type':'boolean','nullable':'NO'}] if column else []}}

    def test_unknown_production_branch_is_blocked(self):
        result = gate.evaluate(self.evidence('9999_unknown_production_branch', False))
        self.assertEqual(result['decision'], 'BLOCKED')
        self.assertEqual(result['migration_path'], [])

    def test_matching_revision_without_required_column_is_blocked(self):
        self.assertEqual(gate.evaluate(self.evidence(column=False))['decision'], 'BLOCKED')

    def test_known_head_with_column_passes_only_schema_gate(self):
        result = gate.evaluate(self.evidence())
        self.assertEqual(result['decision'], 'SCHEMA_GATE_PASS')
        self.assertFalse(result['deployment_authorized'])

    def test_known_ancestor_requires_exact_migration(self):
        result = gate.evaluate(self.evidence('0018_playback_geo_country_policy', False))
        self.assertEqual(result['decision'], 'MIGRATION_REQUIRED')
        self.assertEqual(result['migration_path'], ['0019_explicit_test_content'])

    def test_missing_read_only_evidence_is_blocked(self):
        evidence = self.evidence(); evidence['database']['transaction_read_only'] = 'off'
        self.assertEqual(gate.evaluate(evidence)['decision'], 'BLOCKED')

    def test_multiple_production_heads_are_blocked(self):
        evidence = self.evidence(); evidence['database']['revisions'].append('another_head')
        self.assertEqual(gate.evaluate(evidence)['decision'], 'BLOCKED')


if __name__ == '__main__':
    unittest.main()
