"""Fail closed when read-only production evidence does not match this lineage.

This checks captured catalog metadata only. It never connects to a database,
executes migrations or grants permission to deploy.
"""
import argparse
import json
from pathlib import Path

from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]


def evaluate(evidence, migrations=None):
    script = ScriptDirectory(str(migrations or ROOT / 'backend/migrations'))
    heads = script.get_heads()
    db = evidence.get('database', {})
    revisions = db.get('revisions', [])
    result = {'local_heads': heads, 'observed_production_revisions': revisions,
              'decision': 'BLOCKED', 'deployment_authorized': False, 'migration_path': []}
    if db.get('transaction_read_only') != 'on' or len(revisions) != 1 or len(heads) != 1:
        return dict(result, reason='Missing verified read-only catalog evidence or non-single migration head')
    revision, target = revisions[0], heads[0]
    try:
        script.get_revision(revision)
        path = list(reversed(list(script.iterate_revisions(target, revision))))
    except Exception as error:
        # Do not invent a migration number, stamp or downgrade an unknown branch.
        return dict(result, reason='Production revision is unknown to, or not an ancestor of, the local target',
                    error_type=type(error).__name__)
    result['migration_path'] = [node.revision for node in path]
    if path:
        return dict(result, decision='MIGRATION_REQUIRED', reason='Known ancestor; migration and recovery gates still required')
    if target == '0019_explicit_test_content':
        column = next((c for c in db.get('columns', []) if c.get('table') == 'videos' and c.get('column') == 'is_test_content'), None)
        if not column or column.get('type') != 'boolean' or column.get('nullable') != 'NO':
            return dict(result, reason='Revision marker does not match the required non-null boolean videos.is_test_content column')
    return dict(result, decision='SCHEMA_GATE_PASS', reason='Known local head and required catalog shape; other deployment gates remain')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = evaluate(json.loads(args.evidence.read_text(encoding='utf-8-sig')))
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    print(text)
    return 0 if result['decision'] == 'SCHEMA_GATE_PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
