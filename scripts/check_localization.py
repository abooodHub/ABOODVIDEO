"""Validate shared Arabic/English catalogs and template literals without a database."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jinja2 import Environment, FileSystemLoader
from shared.i18n import WEB_DIR, TemplateLiterals, catalog, configure_environment


def main():
    messages = catalog()
    browser = (WEB_DIR / 'catalog.js').read_text(encoding='utf-8')
    assert json.loads(browser.split('=', 1)[1].strip().rstrip(';')) == messages
    for key, value in messages.items():
        assert value.strip(), key
        assert set(re.findall(r'\{\d+\}', key)) == set(re.findall(r'\{\d+\}', value)), key
    count = 0
    for folder, names in ((ROOT / 'backend/app/templates', None),
                          (ROOT / 'studio/web/templates', ['index_v3.html'])):
        env = configure_environment(Environment(loader=FileSystemLoader(folder)))
        for path in folder.glob('*.html'):
            if names is not None and path.name not in names:
                continue
            source = path.read_text(encoding='utf-8-sig').replace('\r\n', '\n')
            missing = TemplateLiterals(source).messages - messages.keys()
            assert not missing, (path.name, missing)
            env.get_template(path.name)
            count += 1
    print(f'Localization valid: {len(messages)} messages, {count} templates, matching browser catalog.')


if __name__ == '__main__':
    main()
