#!/usr/bin/env python3
"""Build the static search index the site runs on.

The searchable unit is a passage: one speaker turn from a transcript, or one
submission's worth of written testimony. A hit is rendered with the passages
either side of it, which is what makes a question and its answer read as one
exchange.

Layout under site/data/:
  meta.json            corpus counts, facets, build date
  hearings.json        one record per hearing, for filters and grouping
  psg/<n>.json         passage text in blocks of 256, fetched only when shown
  idx/<n>.json         postings, sharded by a hash of the term

Positions are counted over every token including stopwords, but only content
words are indexed. Phrase queries therefore still work -- "right to counsel"
matches on right@i and counsel@i+2 -- without paying to store postings for
"to", which would dwarf the rest of the index. Positions are also dropped for
terms that appear in a large share of all passages, where they would cost more
than the phrase search they buy; the client says so when a phrase falls back to
matching its words separately.
"""
import collections, json, math, os, re, sys, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
RAW = os.path.join(ROOT, 'data', 'raw')
TXT = os.path.join(ROOT, 'data', 'text')
OUT = os.path.join(ROOT, 'docs', 'data')

SHARDS = 1024
BLOCK = 128          # passages per text block
K1, B = 1.4, 0.72    # BM25

STOP = set("""a an and are as at be been but by for from had has have he her his i if in
into is it its of on or she that the their them they this to was were will with you your
we our us not no so do does did been being at over under about after before between
than then there here what which who whom whose when where why how all any both each few
more most other some such only own same too very can just should now""".split())

TOKEN = re.compile(r"[a-z0-9][a-z0-9''\-\.]*")


def tokens(text):
    t = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower()
    for m in TOKEN.finditer(t):
        w = m.group(0).strip(".-'")
        if w:
            yield w


SPEAKER_NOISE = {'council', 'member', 'chairperson', 'chair', 'chairman', 'chairwoman',
                 'speaker', 'commissioner', 'deputy', 'first', 'assistant', 'associate',
                 'acting', 'chief', 'director', 'executive', 'president', 'counsel',
                 'committee', 'sergeant', 'at', 'arms', 'clerk', 'the', 'of', 'and',
                 'mr', 'ms', 'mrs', 'miss', 'dr', 'male', 'female', 'unidentified',
                 'inspector', 'administrator', 'chancellor', 'mayor', 'general',
                 'testimony', 'statement', 'new', 'york', 'city'}


def speaker_key(name):
    """The searchable words in a speaker label, minus the titles everyone shares."""
    out = []
    for w in tokens(name or ''):
        if w in SPEAKER_NOISE or len(w) < 2:
            continue
        if w not in out:
            out.append(w)
    return out[:6]


def shard_of(term):
    h = 2166136261
    for ch in term:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h % SHARDS


# ---------------------------------------------------------------- testimony
# Council uploads written testimony either one witness per file or as a single
# packet running to a couple of hundred pages. Packets have no consistent
# separator: submissions open with "Testimony of ...", "REBNY Testimony |",
# "Dear Chair ...", a forwarded email header, or simply "My name is ...".
SUB_OPENER = re.compile(r"""^(?:
      (?:written\s+)?(?:testimony|statement|remarks|comments)\b
    | dear\s+(?:chair|chairs|council|deputy|speaker|hon\.?|honorable|members|committee)
    | to\s+the\s+honorable
    | from:\s
    | my\s+name\s+is\b
    | [A-Z][A-Za-z&.\- ]{1,34}\s+testimony\b
  )""", re.I | re.X)

AUTHOR_RULES = [
    re.compile(r'\b(?:written\s+)?(?:testimony|statement|remarks|comments)\s+'
               r'(?:of|from|by|for)\s+(.{3,140})', re.I),
    re.compile(r'\bmy\s+name\s+is\s+(.{3,90})', re.I),
    re.compile(r'^\s*(?:from:\s*to:\s*subject:\s*date:\s*)(.{3,60})', re.I),
    re.compile(r'^\s*([A-Z][A-Za-z&.\- ]{1,34})\s+testimony\b'),
    re.compile(r'\bI\s+am\s+([A-Z][^,.\n]{2,70})'),
]

# Cut the witness off where the salutation starts; the full heading is kept at
# the top of the passage either way.
CUT_AUTHOR = re.compile(
    r'\s+(?:before|to\s+the|submitted|regarding|re|concerning|'
    r'my\s+name\s+is|i\s+am|and\s+i\s+am|on\s+behalf\s+of\s+the\s+committee|'
    r'oversight|hearing\s+on|for\s+the\s+hearing|in\s+support\s+of|in\s+opposition|'
    r'new\s+york\s+city\s+council|august|september|october|november|december|'
    r'january|february|march|april|may|june|july)\b', re.I)


# A captured author has to look like a person or an organisation. Packets are
# full of sentences that fit the opener patterns -- "I offer this testimony for
# the record" -- and crediting a passage to a sentence fragment is worse than
# crediting it to nobody.
NOT_A_NAME = re.compile(
    r'^(?:i\b|we\b|this\b|that\b|the\s+record|record\b|today\b|behalf\b|'
    r'thank|please|your|these|those|it\b|there\b|chapter|title|comments?\b|'
    r'closing\b|attached\b|below\b|following\b|above\b|dear\b|hello\b|good\b|'
    r'testimony\b|statement\b|support\b|opposition\b|new\s+yorkers?\b)', re.I)


def author_from(raw):
    # Stop at the end of the first real sentence, but not at an initial:
    # "Beverly Solow. I live in Inwood" cuts, "Jumaane D. Williams" does not.
    raw = re.split(r'(?<=[a-z]{2})\.\s+', raw, 1)[0]
    raw = re.split(r'\s+I\s+(?:live|am|have|would|write|writing|urge)\b', raw, 1)[0]
    a = CUT_AUTHOR.split(raw, 1)[0]
    a = re.sub(r'\s+', ' ', a).strip(' .,;:-|')
    a = re.sub(r'^the\s+', '', a, flags=re.I).strip(' .,;:-|')
    a = re.split(r'[,;]\s*(?:and\s+)?I\b', a)[0]
    a = re.split(r'\s+at\s+(?:council|the\s+council|a\s+)', a, 1, re.I)[0]
    a = re.sub(r'[,;]?\s+(?:and|or|but|who|which|that|with|for|on|in)$', '', a,
               flags=re.I).strip(' .,;:-|')
    if len(a) < 3 or NOT_A_NAME.match(a):
        return None
    if not re.match(r"[A-Z]", a):          # a name or an organisation is capitalised
        return None
    if len(a.split()) > 9:                 # a sentence, not a byline
        return None
    if not re.search(r'[A-Za-z]{3}', a):
        return None
    return a[:80]


def author_of(block):
    head = block[:1400]
    for rule in AUTHOR_RULES:
        m = rule.search(head)
        if m:
            a = author_from(m.group(1))
            if a:
                return a
    return None


def split_testimony(text, label):
    """Break a packet into submissions on whichever opener it happens to use.

    Splitting slightly too eagerly is the safe error: a passage is chunked to a
    readable length anyway, and the only thing a boundary decides is which
    witness the passage is credited to. Splitting too rarely credits one
    witness with another's words.
    """
    paras = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    if not paras:
        return [{'author': None, 'heading': label, 'text': text}]

    starts = [i for i, p in enumerate(paras) if SUB_OPENER.match(p)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)

    subs = []
    for k, i in enumerate(starts):
        j = starts[k + 1] if k + 1 < len(starts) else len(paras)
        body = '\n\n'.join(paras[i:j]).strip()
        if not body:
            continue
        if len(body) < 200 and subs:
            subs[-1]['text'] += '\n\n' + body
            continue
        author = author_of(body)
        if subs and author and author == subs[-1]['author']:
            subs[-1]['text'] += '\n\n' + body       # same witness, keep together
            continue
        subs.append({'author': author, 'heading': paras[i][:160], 'text': body})
    return subs or [{'author': None, 'heading': label, 'text': text}]


def chunk(text, target=1100):
    """Split a long submission into readable passages on paragraph breaks."""
    paras = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    if not paras:
        return []
    out, cur = [], ''
    for p in paras:
        if cur and len(cur) + len(p) > target:
            out.append(cur); cur = p
        else:
            cur = (cur + '\n\n' + p) if cur else p
    if cur:
        out.append(cur)
    return out


def main():
    os.makedirs(os.path.join(OUT, 'idx'), exist_ok=True)
    os.makedirs(os.path.join(OUT, 'psg'), exist_ok=True)
    for d in ('idx', 'psg'):
        for f in os.listdir(os.path.join(OUT, d)):
            os.remove(os.path.join(OUT, d, f))

    hearings = [json.loads(l) for l in open(os.path.join(RAW, 'hearings.jsonl'))]
    hearings.sort(key=lambda h: h['date'], reverse=True)

    passages = []          # (hearing_idx, kind, speaker, role, member, text, seq)
    hmeta = []
    speaker_counts = collections.Counter()
    committee_counts = collections.Counter()

    for hi, h in enumerate(hearings):
        first = len(passages)
        seq = 0
        for t in h['turns']:
            passages.append({
                'h': hi, 'k': 'q', 's': t['speaker'], 'r': t['role'],
                'm': t.get('member'), 't': t['text'], 'i': seq,
            })
            seq += 1
            speaker_counts[t['speaker']] += 1

        for doc in h.get('testimony', []):
            p = os.path.join(TXT, f"{doc['file_id']}.txt")
            if not os.path.exists(p):
                continue
            body = open(p).read()
            for sub in split_testimony(body, doc['label']):
                for part in chunk(sub['text']):
                    # Where no witness could be identified, say so rather than
                    # showing the first paragraph as though it were a byline.
                    passages.append({
                        'h': hi, 'k': 'w',
                        's': sub['author'] or f"{doc['label']} (witness not identified)",
                        'r': 'written', 'm': None, 't': part, 'i': seq,
                    })
                    seq += 1

        hm = {
            'id': h['id'], 'date': h['date'], 'committees': h['committees'],
            'stated': bool(h.get('stated_meeting')), 'time': h.get('start_time'),
            'first': first, 'n': len(passages) - first,
            'docs': [{'k': d['kind'], 'l': d['label'], 'u': d['url'],
                      'p': d.get('pages')} for d in h['documents']],
        }
        hmeta.append(hm)
        for c in h['committees']:
            committee_counts[c] += 1

    print(f'{len(hearings)} hearings, {len(passages)} passages', flush=True)
    if not passages:
        print('FAILED: no passages'); sys.exit(1)

    # ---- postings ----
    postings = collections.defaultdict(list)   # term -> [(pid, tf, [positions])]
    lengths = []
    for pid, p in enumerate(passages):
        pos = collections.defaultdict(list)
        n = 0
        for i, w in enumerate(tokens(p['t'])):
            n += 1
            if w in STOP or len(w) > 32:
                continue
            pos[w].append(i)
        lengths.append(max(n, 1))
        for w, ps in pos.items():
            postings[w].append((pid, len(ps), ps))
        # A facet lives in the index as a reserved term, so filtering by who is
        # speaking is a posting-list intersection rather than a pass over the
        # passage text -- which at this size would mean fetching the corpus.
        # The underscore keeps it out of reach of the tokeniser.
        postings[f'zzrole_{p["r"]}'].append((pid, 1, [0]))
        for w in speaker_key(p.get('m') or p['s']):
            postings[f'zzspk_{w}'].append((pid, 1, [0]))
        if pid % 100000 == 0 and pid:
            print(f'  indexed {pid}/{len(passages)}', flush=True)

    avg = sum(lengths) / len(lengths)
    N = len(passages)

    # Positions are what make a quoted phrase exact. Dropping them for common
    # words breaks exactly the phrases people search for -- "right to counsel",
    # "class size" -- so the cutoff is deliberately high and only the handful of
    # terms that appear in most passages lose theirs.
    pos_cutoff = int(os.environ.get('POS_CUTOFF', max(4000, int(0.60 * N))))

    shards = collections.defaultdict(dict)
    n_nopos = 0
    for term, plist in postings.items():
        df = len(plist)
        keep_pos = df <= pos_cutoff
        if not keep_pos:
            n_nopos += 1
        flat = [df, 1 if keep_pos else 0]
        prev = 0
        for pid, tf, ps in plist:
            L = lengths[pid]
            # BM25 term weight without idf, quantised to a byte so the client
            # needs no per-passage length table
            w = tf * (K1 + 1) / (tf + K1 * (1 - B + B * L / avg))
            wq = max(1, min(255, int(round(w / (K1 + 1) * 255))))
            flat.append(pid - prev); prev = pid
            flat.append(wq)
            if keep_pos:
                flat.append(tf)
                pp = 0
                for x in ps:
                    flat.append(x - pp); pp = x
        shards[shard_of(term)][term] = flat
    print(f'  positions dropped for {n_nopos} terms above df {pos_cutoff}', flush=True)

    # The whole vocabulary, small enough to ship once and search in the browser.
    # It is what makes wildcard, fuzzy matching and "did you mean" possible
    # without a server: expansion happens against this, then the expanded terms
    # are looked up in the shards as normal.
    vocab = sorted((t, len(pl)) for t, pl in postings.items()
                   if not t.startswith('zz') and len(pl) >= 2 and len(t) > 1)
    json.dump(vocab, open(os.path.join(OUT, 'dict.json'), 'w'),
              separators=(',', ':'), ensure_ascii=False)
    dict_bytes = os.path.getsize(os.path.join(OUT, 'dict.json'))
    print(f'dictionary: {len(vocab)} terms, {dict_bytes/1e6:.1f} MB', flush=True)

    # every speaker we indexed, for the speaker: filter's autocomplete
    speakers = sorted({t[len('zzspk_'):] for t in postings if t.startswith('zzspk_')})
    json.dump(speakers, open(os.path.join(OUT, 'speakers.json'), 'w'),
              separators=(',', ':'), ensure_ascii=False)

    total = 0
    for s in range(SHARDS):
        path = os.path.join(OUT, 'idx', f'{s}.json')
        with open(path, 'w') as f:
            json.dump(shards.get(s, {}), f, separators=(',', ':'))
        total += os.path.getsize(path)
    print(f'index: {len(postings)} terms, {total/1e6:.1f} MB across {SHARDS} shards',
          flush=True)

    # ---- passage text blocks ----
    # Speaker names repeat constantly within a hearing, and a passage's hearing
    # and position are both derivable from its id, so a block stores interned
    # strings and bare triples rather than a list of objects. Over a corpus this
    # size the field names alone would run to tens of megabytes.
    psg_bytes = 0
    for b in range(0, len(passages), BLOCK):
        blk = passages[b:b + BLOCK]
        sp, spi = [], {}
        ro, roi = [], {}
        rows = []
        for p in blk:
            name = p['m'] or p['s']
            if name not in spi:
                spi[name] = len(sp); sp.append(name)
            if p['r'] not in roi:
                roi[p['r']] = len(ro); ro.append(p['r'])
            rows.append([spi[name], roi[p['r']], p['t']])
        path = os.path.join(OUT, 'psg', f'{b // BLOCK}.json')
        with open(path, 'w') as f:
            json.dump({'sp': sp, 'ro': ro, 'p': rows}, f,
                      separators=(',', ':'), ensure_ascii=False)
        psg_bytes += os.path.getsize(path)
    print(f'passage blocks: {psg_bytes/1e6:.1f} MB', flush=True)

    json.dump(hmeta, open(os.path.join(OUT, 'hearings.json'), 'w'),
              separators=(',', ':'), ensure_ascii=False)

    dates = sorted(h['date'] for h in hmeta)
    n_meetings = sum(1 for _ in open(os.path.join(RAW, 'meetings.jsonl')))
    # Be explicit about what did not make it in, rather than quietly shipping a
    # smaller corpus than the crawl found.
    fs = json.load(open(os.path.join(RAW, 'fetch_state.json')))
    excluded = collections.Counter(v.get('status') for v in fs.values())
    excluded.pop('ok', None)
    n_docs_total = sum(1 for _ in open(os.path.join(RAW, 'documents.jsonl')))
    n_dupes = sum(len(h.get('duplicate_file_ids', []))
                  for h in (json.loads(l) for l in
                            open(os.path.join(RAW, 'hearings.jsonl'))))
    n_candidates = 0
    for line in open(os.path.join(RAW, 'documents.jsonl')):
        d = json.loads(line)
        n_candidates += 1 + len(d.get('aliases', []))
    meta = {
        'built': __import__('datetime').date.today().isoformat(),
        'meetings': n_meetings,
        'candidates': n_candidates,
        'documents_selected': n_docs_total,
        'duplicates_collapsed': n_dupes,
        'excluded': dict(excluded),
        'excluded_total': sum(excluded.values()),
        'hearings': len(hmeta),
        'passages': N,
        'turns': sum(1 for p in passages if p['k'] == 'q'),
        'written': sum(1 for p in passages if p['k'] == 'w'),
        'terms': len(postings),
        'vocab': len(vocab),
        'speakers': len(speakers),
        'shards': SHARDS,
        'block': BLOCK,
        'avgdl': round(avg, 2),
        'first_date': dates[0] if dates else None,
        'last_date': dates[-1] if dates else None,
        'transcripts': sum(1 for h in hmeta for d in h['docs'] if d['k'] == 'transcript'),
        'testimony_docs': sum(1 for h in hmeta for d in h['docs'] if d['k'] == 'testimony'),
        'committees': [c for c, _ in committee_counts.most_common()],
        'top_speakers': speaker_counts.most_common(400),
    }
    json.dump(meta, open(os.path.join(OUT, 'meta.json'), 'w'),
              separators=(',', ':'), ensure_ascii=False)
    print(f"passages {N}  turns {meta['turns']}  written {meta['written']}", flush=True)


if __name__ == '__main__':
    main()
