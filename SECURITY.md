# Security policy

Report suspected vulnerabilities privately to the project maintainer. Include the affected component, impact, and a reproducible description. Do not publish exploit details or real user data before a fix is available.

Never commit `.env`, API keys, signing secrets, passwords, cookies, session tokens, private keys, or production configuration. Use `.env.example` only as a template and generate unique secrets for each deployment. Rotate any credential suspected of exposure; do not put credentials in issue trackers or audit reports.

Treat uploaded archives and media as untrusted input. The platform validates package manifests, archive members, paths, sizes, and media metadata before making a package available. Preserve path traversal protections and run upload tests only against isolated storage and services.

Keep production PostgreSQL, Redis, media storage, and Studio user profiles out of test runs. Report a security issue with the minimum sensitive evidence needed, with secrets redacted.
