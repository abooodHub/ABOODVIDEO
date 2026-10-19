# Contributing

Keep changes focused and preserve stable module paths. Add regression coverage for behavior changes and run the relevant isolated tests before submitting. Do not use real media, production services, production profiles, or credentials in tests; use `tmp_path`, synthetic fixtures, and explicitly isolated PostgreSQL/Redis. Follow the existing Python and JavaScript style, and run `git diff --check`. Never stage generated builds, runtime databases, logs, or font/media files with unclear redistribution rights.
