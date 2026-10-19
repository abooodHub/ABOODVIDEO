"""Language preference with no authentication or database changes."""
from http.cookies import SimpleCookie
import json
from urllib.parse import parse_qs

from . import COOKIE_NAME, SUPPORTED, gettext, use_language


class LocaleMiddleware:
    def __init__(self, app, secure_cookie=False, fallback=None):
        self.app = app
        self.secure_cookie = secure_cookie
        self.fallback = fallback

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        cookies = SimpleCookie()
        try:
            cookies.load(headers.get(b'cookie', b'').decode('latin-1'))
        except Exception:
            pass
        query = parse_qs(scope.get('query_string', b'').decode('ascii', 'ignore'))
        explicit = query.get('lang', [''])[0]
        cookie = cookies.get(COOKIE_NAME)
        selected = explicit if explicit in SUPPORTED else cookie.value if cookie and cookie.value in SUPPORTED else None
        if selected is None:
            selected = self.fallback() if self.fallback else 'ar'
        selected = selected if selected in SUPPORTED else 'ar'
        start = None
        body = bytearray()

        async def localized_send(message):
            nonlocal start
            if message['type'] == 'http.response.start':
                message = {**message, 'headers': list(message.get('headers', []))}
                response_headers = dict(message['headers'])
                content_type = response_headers.get(b'content-type', b'')
                if b'text/html' in content_type:
                    message['headers'].append((b'content-language', selected.encode()))
                    previous = response_headers.get(b'vary', b'')
                    message['headers'] = [(k,v) for k,v in message['headers'] if k.lower() != b'vary']
                    message['headers'].append((b'vary', previous + (b', ' if previous else b'') + b'Cookie'))
                if explicit in SUPPORTED and not scope.get('path', '').startswith('/static/'):
                    value = f'{COOKIE_NAME}={selected}; Path=/; Max-Age=31536000; SameSite=Lax'
                    if self.secure_cookie:
                        value += '; Secure'
                    message['headers'].append((b'set-cookie', value.encode()))
                if selected == 'en' and message['status'] >= 400 and b'application/json' in content_type:
                    start = message
                else:
                    await send(message)
                return
            if message['type'] == 'http.response.body' and start is not None:
                body.extend(message.get('body', b''))
                if message.get('more_body'):
                    return
                try:
                    data = json.loads(body)
                    # Only system error envelopes, never media titles or user records.
                    if isinstance(data, dict):
                        for key in ('message', 'detail'):
                            if isinstance(data.get(key), str):
                                data[key] = gettext(data[key])
                        if isinstance(data.get('error'), dict) and isinstance(data['error'].get('message'), str):
                            data['error']['message'] = gettext(data['error']['message'])
                    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
                except (ValueError, UnicodeDecodeError):
                    payload = bytes(body)
                start['headers'] = [(k,v) for k,v in start['headers'] if k.lower() != b'content-length']
                start['headers'].append((b'content-length', str(len(payload)).encode()))
                await send(start)
                await send({'type':'http.response.body', 'body':payload})
                return
            await send(message)

        with use_language(selected):
            await self.app(scope, receive, localized_send)
