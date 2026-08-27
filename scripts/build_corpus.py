#!/usr/bin/env python3
"""Assemble hearings from the fetched documents.

A hearing is the unit a reader cares about: one committee (or several sitting
jointly) meeting on one day. Documents are grouped onto hearings by the date
and committee the document itself declares -- not by which meeting page we
happened to find it on, because Legistar attaches a bill's whole document
history to every later meeting that touches the bill.
"""
import collections, hashlib, json, os, re, sys, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
RAW = os.path.join(ROOT, 'data', 'raw')
TXT = os.path.join(ROOT, 'data', 'text')
sys.path.insert(0, HERE)
import segment

VIDEO = re.compile(r'Video')

# Legistar has hearings back to the 1990s but this corpus was enumerated from
# the 2024-2026 sessions; anything outside this window is a mis-parse.
PLAUSIBLE_FIRST = '2010-01-01'
PLAUSIBLE_LAST = (__import__('datetime').date.today() +
                  __import__('datetime').timedelta(days=120)).isoformat()


def norm_key(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '', s.lower())


def load_members():
    p = os.path.join(RAW, 'council_members_current.json')
    if not os.path.exists(p):
        return {}
    out = {}
    for m in json.load(open(p)):
        full = m['name']
        bare = unicodedata.normalize('NFKD', full).encode('ascii', 'ignore').decode()
        bare = re.sub(r'\s+(Jr|Sr|II|III)\.?$', '', bare).strip()
        toks = bare.split()
        # index by last token and by trailing multi-word surnames (De La Rosa)
        for n in (1, 2, 3):
            if len(toks) > n:
                out.setdefault(' '.join(toks[-n:]).upper(), full)
        out.setdefault(toks[-1].upper(), full)
    return out


def main():
    docs = {d['file_id']: d for d in
            (json.loads(l) for l in open(os.path.join(RAW, 'documents.jsonl')))}
    state = json.load(open(os.path.join(RAW, 'fetch_state.json')))
    members = load_members()

    # meeting records give us committee/location/video for a date
    meetings = {}
    for line in open(os.path.join(RAW, 'meetings.jsonl')):
        r = json.loads(line)
        meetings[r['meeting_id']] = r

    hearings = {}
    by_hash = {}
    stats = collections.Counter()

    for fid, st in state.items():
        if st.get('status') != 'ok':
            stats['doc_' + st.get('status', 'missing')] += 1
            continue
        doc = docs.get(fid)
        if not doc:
            continue
        tp = os.path.join(TXT, f'{fid}.txt')
        if not os.path.exists(tp):
            stats['missing_text'] += 1
            continue
        text = open(tp).read()

        # final duplicate collapse: identical bytes, whatever the file id
        h = st.get('sha1')
        if h and h in by_hash:
            hearings[by_hash[h]]['duplicate_file_ids'].append(fid)
            stats['duplicate'] += 1
            continue

        pages = text.split('\n\n')
        cover = segment.parse_cover(pages[:4]) if doc['kind'] == 'transcript' else {}
        meeting_dates = sorted(s['date'] for s in doc['seen_at'] if s.get('date'))
        # A two-digit year in a label is ambiguous and Legistar's labels contain
        # typos: "Hearing Transcript 10/23/30" is not a hearing in 2030. Take the
        # first date that is actually plausible, preferring the document's own
        # cover page over its filename over the meeting it hung off.
        date = None
        for cand in (cover.get('date'), doc.get('label_date'),
                     meeting_dates[0] if meeting_dates else None):
            if cand and PLAUSIBLE_FIRST <= cand <= PLAUSIBLE_LAST:
                date = cand
                break
        if not date:
            stats['implausible_date'] += 1
            continue

        committees = cover.get('committees') or []
        if not committees:
            cands = [s['committee'] for s in doc['seen_at']
                     if s.get('date') == date and s.get('committee')]
            if cands:
                committees = [collections.Counter(cands).most_common(1)[0][0]]
        if not committees:
            committees = ['New York City Council']

        key = date + '|' + norm_key('+'.join(sorted(committees)))
        hr = hearings.setdefault(key, {
            'id': hashlib.sha1(key.encode()).hexdigest()[:12],
            'date': date,
            'committees': committees,
            'stated_meeting': bool(cover.get('stated_meeting')) or doc.get('stated'),
            'start_time': cover.get('start_time'),
            'documents': [],
            'duplicate_file_ids': [],
            'turns': [],
            'testimony': [],
        })
        if h:
            by_hash[h] = key

        meeting_ids = sorted({s['meeting_id'] for s in doc['seen_at']})
        hr['documents'].append({
            'file_id': fid, 'kind': doc['kind'], 'label': doc['label'],
            'url': doc['url'], 'pages': st.get('pages'), 'chars': st.get('chars'),
            'meeting_ids': meeting_ids[:8],
        })

        if doc['kind'] == 'transcript':
            turns = segment.split_turns(text)
            for t in turns:
                if t['role'] == 'council':
                    t['member'] = members.get(t['name'])
                t['file_id'] = fid
            hr['turns'].extend(turns)
            stats['transcripts'] += 1
            stats['turns'] += len(turns)
        elif doc['kind'] == 'testimony':
            hr['testimony'].append({'file_id': fid, 'label': doc['label'],
                                    'chars': len(text)})
            stats['testimony_docs'] += 1

    out = os.path.join(RAW, 'hearings.jsonl')
    with open(out, 'w') as f:
        for k in sorted(hearings, reverse=True):
            f.write(json.dumps(hearings[k]) + '\n')

    print(f'{len(hearings)} hearings', flush=True)
    for k, v in stats.most_common():
        print(f'   {k}: {v}', flush=True)
    if not hearings:
        print('FAILED: no hearings assembled', flush=True)
        sys.exit(1)

    # data/text/ is not in git, so a fresh clone has fetch_state.json saying
    # every document is done and no text to show for it. Fail rather than
    # quietly building a corpus with most of the record missing.
    expected = sum(1 for v in state.values() if v.get('status') == 'ok')
    if stats['missing_text'] > expected * 0.05:
        print(f"FAILED: {stats['missing_text']} of {expected} documents marked fetched "
              f"have no text in data/text/. Re-run scripts/fetch_pdfs.py.", flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
