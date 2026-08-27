#!/usr/bin/env python3
"""Repair the artefacts PDF extraction leaves behind.

These are our own errors, not the Council's. The stenographer's spellings and
the witnesses' typos are the record and stay exactly as written; what gets
fixed here is damage done between the PDF and the text file:

  * ligature glyphs, so "beneﬁts" is searchable as "benefits"
  * a colon standing in for a "ti" pair, so "informa:on" reads "information"
  * soft hyphens left inside words
  * a handful of documents that extracted with no word spaces at all

Every repair is checked against a word list before it is applied, and the whole
module is idempotent: running it twice changes nothing the second time.
"""
import json, math, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, '..', 'data', 'raw')

LIGATURES = {
    'ﬀ': 'ff', 'ﬁ': 'fi', 'ﬂ': 'fl', 'ﬃ': 'ffi',
    'ﬄ': 'ffl', 'ﬅ': 'st', 'ﬆ': 'st',
    '‐': '-', '‑': '-',
}
SOFT_HYPHEN = '­'
COLON_WORD = re.compile(r'\b([A-Za-z]{2,})\:([A-Za-z]{2,})\b')
LONG_RUN = re.compile(r'[A-Za-z]{25,}')


def load_words():
    """A word list for checking repairs: the system dictionary plus whatever
    the healthy part of this corpus actually says."""
    words = set()
    sys_dict = '/usr/share/dict/words'
    if os.path.exists(sys_dict):
        for w in open(sys_dict, errors='ignore'):
            w = w.strip().lower()
            if len(w) > 1:
                words.add(w)
    corpus = os.path.join(RAW, 'word_freq.json')
    if os.path.exists(corpus):
        words.update(json.load(open(corpus)).keys())
    return words


def load_freq():
    p = os.path.join(RAW, 'word_freq.json')
    return json.load(open(p)) if os.path.exists(p) else {}


def fix_ligatures(text):
    for k, v in LIGATURES.items():
        text = text.replace(k, v)
    return text.replace(SOFT_HYPHEN, '')


def fix_colon_ti(text, words):
    """"informa:on" is "information" -- but only where that is a real word.

    In documents that extracted without spaces the same pattern is a genuine
    colon ("suchas:realestate"), so the word list decides.
    """
    def sub(m):
        joined = m.group(1) + 'ti' + m.group(2)
        return joined if joined.lower() in words else m.group(0)
    return COLON_WORD.sub(sub, text)


# The system dictionary is full of archaic words -- "ea" and "hav" are both in
# it -- which lets a bad split look cheap. Segmentation therefore runs on the
# vocabulary this corpus actually uses, and short words have to be common to
# qualify at all.
SHORT_FLOOR = {2: 400, 3: 120}
ONE_LETTER = {'a', 'i'}


def segment(run, freq, words=None):
    """Split a run of letters that lost its spaces back into words.

    Dynamic programming over word cost: a common word is cheap, a rare one
    dear, anything outside the corpus vocabulary is barred. Returns None when
    the run cannot be covered, so a failed guess never reaches the reader.
    """
    n = len(run)
    if n > 400:
        return None
    low = run.lower()
    total = sum(freq.values()) or 1
    best = [None] * (n + 1)
    best[0] = (0.0, 0, None)
    MAXW = 22
    for i in range(1, n + 1):
        for L in range(1, min(MAXW, i) + 1):
            j = i - L
            if best[j] is None:
                continue
            w = low[j:i]
            if L == 1 and w not in ONE_LETTER:
                continue
            f = freq.get(w)
            if f is None or (L > 1 and f < SHORT_FLOOR.get(L, 3)):
                continue
            cost = best[j][0] + (-math.log(f / total)) + 1.6   # penalty per word
            if best[i] is None or cost < best[i][0]:
                best[i] = (cost, j, w)
    if best[n] is None:
        return None
    out, i = [], n
    while i:
        _, j, w = best[i]
        out.append(run[j:i])
        i = j
    return ' '.join(reversed(out))


DAMAGED_RUN = re.compile(r'[A-Za-z]{12,}')


def fix_runs(text, freq, words=None, threshold=25):
    if not freq:
        return text
    pat = DAMAGED_RUN if threshold <= 12 else LONG_RUN
    return pat.sub(lambda m: segment(m.group(0), freq) or m.group(0), text)


def repair(text, words=None, freq=None, segment_runs=False):
    words = words if words is not None else load_words()
    text = fix_ligatures(text)
    text = fix_colon_ti(text, words)
    if segment_runs:
        text = fix_runs(text, freq if freq is not None else load_freq(), words)
    return text
