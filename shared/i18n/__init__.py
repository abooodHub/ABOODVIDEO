"""Explicit UI localization. Template literals are translated before rendering;
user titles, filenames, subtitles and other interpolated data are never scanned.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / 'web'
COOKIE_NAME = 'av_language'
SUPPORTED = ('ar', 'en')
_language = ContextVar('av_ui_language', default='ar')
ARABIC = re.compile(r'[\u0621-\u064a\u066e-\u06d3]')


def language():
    return _language.get()


def direction():
    return 'ltr' if language() == 'en' else 'rtl'


@contextmanager
def use_language(value):
    token = _language.set(value if value in SUPPORTED else 'ar')
    try:
        yield
    finally:
        _language.reset(token)


def normalize(value):
    return re.sub(r'\s+', ' ', html.unescape(str(value))).strip()


@lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / 'en.json').read_text(encoding='utf-8'))


@lru_cache(maxsize=1)
def message_patterns():
    result = []
    for key, value in catalog().items():
        parts = re.split(r'(\{\d+\})', key)
        if len(parts) == 1:
            continue
        slots = [p[1:-1] for p in parts if re.fullmatch(r'\{\d+\}', p)]
        pattern = ''.join('(.+?)' if re.fullmatch(r'\{\d+\}', p) else re.escape(p) for p in parts)
        result.append((re.compile('^' + pattern + '$', re.S), slots, value))
    return result


def gettext(message):
    if not isinstance(message, str) or language() == 'ar':
        return message
    key = normalize(message)
    value = catalog().get(key)
    if value is not None:
        return value
    for pattern, slots, translation in message_patterns():
        match = pattern.fullmatch(key)
        if match:
            values = dict(zip(slots, match.groups()))
            return re.sub(r'\{(\d+)\}', lambda m: values.get(m[1], m[0]), translation)
    return message


class TemplateLiterals(HTMLParser):
    """Locate only template-owned text/accessibility attributes by source offsets."""
    attributes = {'title', 'aria-label', 'aria-description', 'placeholder', 'alt', 'label'}
    skip = {'script', 'style', 'code', 'pre'}

    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.edits = []
        self.messages = set()
        self.stack = []
        self.lines = [0]
        self.lines.extend(m.end() for m in re.finditer('\n', source))
        self.data_spans = []
        masked = ['\n' if c == '\n' else ' ' for c in source]
        offset = 0
        for _, kind, value in Environment(keep_trailing_newline=True).lex(source):
            start = source.find(value, offset)
            if start < 0:
                raise ValueError('Unable to locate template literal')
            end = start + len(value)
            if kind == 'data':
                self.data_spans.append((start, end))
                masked[start:end] = value
            offset = end
        # Jinja normalizes CRLF; work in its canonical LF input throughout.
        self.feed(''.join(masked))

    def position(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def literal(self, start, end):
        for left, right in self.data_spans:
            a, b = max(start, left), min(end, right)
            if a >= b:
                continue
            text = self.source[a:b]
            if not ARABIC.search(text):
                continue
            # Interpolated variables occupy non-data spans and are untouched.
            lead = len(text) - len(text.lstrip())
            trail = len(text) - len(text.rstrip())
            key = normalize(text)
            if not key:
                continue
            self.messages.add(key)
            expression = '{{ _(' + json.dumps(key, ensure_ascii=False) + ') }}'
            self.edits.append((a + lead, b - trail, expression))

    def handle_starttag(self, tag, attrs):
        start = self.position()
        raw = self.get_starttag_text()
        for match in re.finditer(r'([\w:-]+)\s*=\s*([\"\'])(.*?)\2', raw, re.S):
            if match[1] in self.attributes:
                self.literal(start + match.start(3), start + match.end(3))
        if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, data):
        if not any(tag in self.skip for tag in self.stack):
            self.literal(self.position(), self.position() + len(data))


def localize_template(source):
    source = source.replace('\r\n', '\n').replace('\r', '\n')
    parser = TemplateLiterals(source)
    for start, end, value in sorted(set(parser.edits), reverse=True):
        source = source[:start] + value + source[end:]
    def localize_defaults(match):
        expression = match[0]
        def replace_default(value):
            text = value[3]
            return value[1] + '_(' + json.dumps(text, ensure_ascii=False) + ')' if ARABIC.search(text) else value[0]
        return re.sub(r'(\|\s*default\(\s*)([\"\'])(.*?)\2', replace_default, expression)
    source = re.sub(r'\{\{[\s\S]*?\}\}', localize_defaults, source)
    source = re.sub(r'(<html\b[^>]*\blang=)[\"\']ar[\"\']', r'\1"{{ html_lang() }}"', source, flags=re.I)
    source = re.sub(r'(<html\b[^>]*\bdir=)[\"\']rtl[\"\']', r'\1"{{ html_dir() }}"', source, flags=re.I)
    assets = ('<script src="/static/i18n/catalog.js?v=1.1.0"></script>'
              '<script src="/static/i18n/runtime.js?v=1.1.0"></script>'
              '<link rel="stylesheet" href="/static/i18n/direction.css?v=1.1.0">')
    return source.replace('</head>', assets + '</head>')


class LocalizedLoader(FileSystemLoader):
    def get_source(self, environment, template):
        source, filename, uptodate = super().get_source(environment, template)
        return localize_template(source), filename, uptodate


def configure_environment(env):
    if isinstance(env.loader, FileSystemLoader) and not isinstance(env.loader, LocalizedLoader):
        env.loader = LocalizedLoader(env.loader.searchpath)
    env.globals.update(_=gettext, html_lang=language, html_dir=direction)
    return env
