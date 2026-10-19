# Pre-0019 schema fixture

`pre_0019.sql` is a reviewed schema-only PostgreSQL 16 dump of the pre-0019 lineage.
It contains table/type/index/constraint definitions and no rows, credentials,
roles or database-creation commands. Its SHA-256 is pinned by
`test_f16_migration_upgrade.py` to detect accidental fixture changes.

The migration test restores it only into a newly created disposable database on
the guarded loopback test service, seeds synthetic records, applies 0019 and
checks preservation and model parity. No production service is contacted.
