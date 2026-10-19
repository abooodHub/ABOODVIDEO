import sys
import asyncio
from playwright.async_api import async_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

async def check_err():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        page.on('console', lambda msg: print(f'[Console] {msg.type}: {msg.text}'))
        page.on('pageerror', lambda err: print(f'[PAGE ERROR]: {err}\nSTACK:\n{getattr(err, "stack", "no stack")}'))
        
        print("Navigating to login...")
        await page.goto('https://platform.example/admin/login', wait_until='networkidle')
        await page.fill('#username', 'abood')
        await page.fill('#password', 'AboodVideo2026!Secure')
        await page.click('button[type="submit"]')
        
        print("Waiting for dashboard navigation...")
        await page.wait_for_url('**/admin', timeout=10000)
        await page.wait_for_selector('a[href="/admin/videos"]', timeout=10000)
        print("Clicking 'مكتبة المحتوى' link in sidebar...")
        await page.click('a[href="/admin/videos"]')
        
        print("Waiting for /admin/videos...")
        await page.wait_for_url('**/admin/videos', timeout=10000)
        await asyncio.sleep(3)
        
        title = await page.title()
        print("PAGE TITLE:", title)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(check_err())
