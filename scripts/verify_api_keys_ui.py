import sys
from pathlib import Path
sys.path.insert(0, r"C:\aboodvideo")
import requests
from scripts.create_test_session import create_session

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

auth = create_session()
session = requests.Session()
session.cookies.set("aboodvideo_session", auth["session"])
session.cookies.set("aboodvideo_csrf", auth["csrf"])

res = session.get("http://127.0.0.1/admin/api-keys")
print(f"Status Code: {res.status_code}")
assert res.status_code == 200, f"Expected 200, got {res.status_code}"

html = res.text
checks = [
    ("packages:write", "value=\"packages:write\"" in html),
    ("packages:create", "value=\"packages:create\"" in html),
    ("packages:publish", "value=\"packages:publish\"" in html),
    ("videos:read", "value=\"videos:read\"" in html),
    ("رفع ملفات الحزمة label", "رفع ملفات الحزمة" in html),
    ("Preset Studio Button", "btn-preset-studio" in html),
    ("Preset Studio Arabic label", "صلاحيات ABOODVIDEO Studio" in html),
]

for label, passed in checks:
    print(f"[{'PASS' if passed else 'FAIL'}] {label}")

css_res = session.get("http://127.0.0.1/static/css/admin_api_keys.css")
js_res = session.get("http://127.0.0.1/static/js/admin_api_keys.js")

print(f"[CSS] btn-preset present: {'btn-preset' in css_res.text}")
print(f"[JS] btn-preset-studio handler present: {'btn-preset-studio' in js_res.text}")
