# Credential rotation

This is a general operator runbook. Keep all incident-specific details, secret values and recovery material outside the repository.

## Before rotating

1. Identify the affected credential and all consumers: application services, workers, integrations, backups and scheduled tasks.
2. Prepare a private recovery plan and confirm access through a separate working administrator account or SSH key.
3. Back up protected configuration. Keep database backups outside source and release assets.

## Rotation sequence

1. Generate a new strong credential using the relevant service or secret manager.
2. Update every consumer without printing connection strings, tokens or passwords.
3. Restart only affected services during a planned maintenance window.
4. Verify readiness, authentication and the required workflows.
5. Revoke the old credential and verify it no longer works.
6. Record the timestamp and operator privately; check protected logs for authentication failures.

Changing a password in a current file does not remove it from existing Git commits, logs, backups or downloaded artifacts. Revoke exposed credentials even when publishing a new clean source repository. Do not rewrite shared history or alter live infrastructure as a side effect of a documentation change.
