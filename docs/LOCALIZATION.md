# Arabic and English

The Platform and Windows Studio support `ar` (default, RTL) and `en` (LTR).
Select a language in the administration header, login page, Studio header or
player menu. Arabic keeps the sidebar on the right; English places it on the
left. Small screens use the existing responsive navigation.

The preference is stored in the `av_language` cookie. Studio also saves
`language` in its local configuration, so a new browser window uses the saved
preference. An explicit `?lang=ar` or `?lang=en` overrides the cookie and saves
the choice. Unsupported values are ignored. Authentication cookies, roles,
access rules, video records and database schema are unaffected.

Changing language reloads the current page. Browser upload pages disable the
selector while files are queued, to avoid losing selected files or interrupting
an upload. Complete or clear the queue first. Studio processing runs in its
backend and its persistent queue is retained across page reloads.

## Translation boundaries

- Only application UI literals and system messages are translated.
- Titles, descriptions, filenames, series/season names, subtitles, fonts,
  API identifiers and saved content remain unchanged.
- Windows picker captions and filters follow the application language.
  Native operating-system chrome follows Windows settings.
- The Windows installer provides Arabic and English independently of the
  application's saved language.
- The root and closed public catalog routes still return empty HTTP 404.

## Contributing translations

The shared dictionary is `shared/i18n/en.json`. Arabic UI literals are message
keys. Keep numbered placeholders such as `{0}` unchanged in the translation.
Regenerate `shared/i18n/web/catalog.js` from the same JSON object, assigning it
to `window.AV_I18N_CATALOG`.

Template-owned text and accessibility attributes are localized before Jinja
rendering using `configure_environment()`. User variables are rendered normally
with existing autoescaping. For literal text inside Jinja expressions or macro
arguments, explicitly call `_('Arabic UI literal')`. Do not call `_` on media
titles or other user values.

In browser code, use `window.AVi18n.t('Arabic UI literal')` or the tagged
template `window.AVi18n.tpl` with JavaScript template syntax. The latter
translates static text before interpolating values. Keep the existing HTML
escaping for values inserted into markup. Do not translate complete rendered
documents or scan user content with a DOM observer.

Server code can use `shared.i18n.gettext` within request context. Do not store
translated values in the database. A pure ASGI middleware isolates the locale
for each request and translates known system error envelope fields only.

## Checks

```sh
python scripts/check_localization.py
python scripts/run_tests.py -q tests/test_localization.py
ABOODVIDEO_RUN_BROWSER_TEST=1 python scripts/run_tests.py -q tests/test_localization_browser.py
```

The test runner requires its documented isolated PostgreSQL/Redis services.
The browser checks render actual templates against synthetic offline fixtures,
check both languages at desktop/mobile widths, inspect sidebar placement and
horizontal overflow, and reject JavaScript errors and missing messages.

Regenerate documentation screenshots without accessing a deployment:

```sh
python scripts/generate_documentation_visuals.py --language ar
python scripts/generate_documentation_visuals.py --language en
```
