#!/usr/bin/env python3
"""PDF -> clean text, for both shapes of document in this corpus.

World Wide Dictation transcripts extract as one word per line with blank lines
between, plus a running committee header and a stenographer footer on every
page. Written testimony is ordinary prose that wraps normally. Detect which
we have and normalise accordingly; get this wrong and either speaker labels
break apart or real paragraphs get welded together.
"""
import os, re, statistics, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FOOTER = re.compile(r'World\s*Wide\s*Dictation.*?WorldWideDictation\.com', re.I | re.S)
PHONE = re.compile(r'Phone:\s*\d{3}[-.]\d{3}[-.]\d{4}.*?Fax:[^\n]*', re.I | re.S)
ADDRESS = re.compile(r'\d+\s+Saw\s+Mill\s+River\s+Road[^\n]*', re.I)
HEADER_LINE = re.compile(r'^(?:[A-Z][A-Z &,\'\-\.\/]{7,}\s*)+\d{0,4}\s*$')
PAGE_NO = re.compile(r'^\d{1,4}\s*$')


def _strip_running_header(lines):
    """Drop the all-caps committee banner and page number at the top of a page."""
    i = 0
    while i < len(lines) and i < 8:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if PAGE_NO.match(s) or HEADER_LINE.match(s):
            i += 1
            continue
        break
    return lines[i:]


def _is_word_per_line(lines):
    real = [l for l in lines if l.strip()]
    if len(real) < 12:
        return False
    lengths = [len(l.strip()) for l in real]
    return statistics.median(lengths) <= 14


def clean_page(raw):
    t = FOOTER.sub(' ', raw)
    t = PHONE.sub(' ', t)
    t = ADDRESS.sub(' ', t)
    lines = [l.rstrip() for l in t.split('\n')]
    lines = _strip_running_header(lines)

    if _is_word_per_line(lines):
        # every line break is a wrapping artifact; the page is one run of prose
        body = ' '.join(l.strip() for l in lines if l.strip())
    else:
        # ordinary prose: blank lines are paragraph breaks, single breaks are wraps
        paras = re.split(r'\n\s*\n', '\n'.join(lines))
        fixed = []
        for p in paras:
            p = re.sub(r'-\n(?=[a-z])', '', p)          # de-hyphenate across a wrap
            p = re.sub(r'\s*\n\s*', ' ', p).strip()
            if p:
                fixed.append(p)
        body = '\n\n'.join(fixed)

    body = re.sub(r'[ \t]{2,}', ' ', body)
    return body.strip()


def extract(path):
    import pypdf
    try:
        from repair import repair, load_words
        words = load_words()
    except Exception:
        repair = None
        words = None
    reader = pypdf.PdfReader(path)
    pages = []
    for p in reader.pages:
        try:
            raw = p.extract_text() or ''
        except Exception:
            raw = ''
        page = clean_page(raw)
        if repair:
            page = repair(page, words=words)
        pages.append(page)
    return pages


if __name__ == '__main__':
    # Called as a subprocess by fetch_pdfs.py so that a malformed PDF can be
    # killed on a timeout: pypdf will happily spin for ever on some files, and
    # in-process that wedges a worker thread permanently.
    pages = extract(sys.argv[1])
    if '--json' in sys.argv:
        import json
        json.dump(pages, sys.stdout)
    else:
        print('\n\n'.join(pages))
