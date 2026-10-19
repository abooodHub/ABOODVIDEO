from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True, locale='ar-SA')
    page = ctx.new_page()
    page.goto('http://127.0.0.1/admin/login', wait_until='networkidle')
    page.fill('#username', 'admin')
    page.fill('#password', 'AdminPass2026!')
    page.click('#login-submit')
    page.wait_for_timeout(2000)

    for path in ['/admin', '/admin/shares']:
        page.goto('http://127.0.0.1' + path, wait_until='networkidle')
        page.wait_for_timeout(1000)
        
        sw_orig = page.evaluate("() => document.documentElement.scrollWidth")
        cw_orig = page.evaluate("() => document.documentElement.clientWidth")
        print(f"Original {path}: scrollW={sw_orig}, clientW={cw_orig}")

        # Check with sidebar hidden
        page.evaluate("() => { const sb = document.getElementById('adminSidebar'); if(sb) sb.style.display = 'none'; }")
        sw_nosb = page.evaluate("() => document.documentElement.scrollWidth")
        cw_nosb = page.evaluate("() => document.documentElement.clientWidth")
        print(f"No sidebar {path}: scrollW={sw_nosb}, clientW={cw_nosb}")

        bad = page.evaluate("""() => {
            const elements = document.querySelectorAll('*');
            const res = [];
            const clientW = document.documentElement.clientWidth;
            for (const el of elements) {
                const rect = el.getBoundingClientRect();
                if (rect.right > clientW + 1 || rect.left < -1) {
                    res.push({
                        tag: el.tagName,
                        id: el.id,
                        class: el.className,
                        right: rect.right,
                        left: rect.left,
                        width: rect.width
                    });
                }
            }
            return res;
        }""")
        for b in bad:
            print("  remaining bad:", b)

    browser.close()
