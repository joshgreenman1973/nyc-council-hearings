#!/usr/bin/env python3
"""Pick the transcripts and written testimony out of the crawled attachments.

The same physical document is uploaded against every bill heard at a hearing,
so the raw attachment list is many times larger than the real corpus. This
HEADs each candidate for its size and filename and collapses duplicates
(same kind + same hearing date + same byte size) down to one representative,
keeping the rest as aliases so nothing is silently dropped.

Input:  data/raw/meetings.jsonl
Output: data/raw/documents.jsonl
"""
import json, os, re, sys, time, threading, queue, warnings, collections
warnings.filterwarnings('ignore')
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, '..', 'data', 'raw')
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}

TRANSCRIPT = re.compile(r'\btranscript\b', re.I)
TESTIMONY = re.compile(r'\btestimon', re.I)
APPEARANCE = re.compile(r'appearance\s+card', re.I)
STATED = re.compile(r'stated\s+meeting', re.I)

# 1/31/24, 1-31-24, 01/31/2024
DATE_IN_LABEL = re.compile(r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})')


def kind_of(label):
    if APPEARANCE.search(label):
        return 'appearance'
    if TRANSCRIPT.search(label):
        return 'transcript'
    if TESTIMONY.search(label):
        return 'testimony'
    return None


def date_in_label(label):
    m = DATE_IN_LABEL.search(label)
    if not m:
        return None
    mm, dd, yy = (int(x) for x in m.groups())
    if yy < 100:
        yy += 2000
    if not (1 <= mm <= 12 and 1 <= dd <= 31 and 2000 <= yy <= 2100):
        return None
    return f'{yy}-{mm:02d}-{dd:02d}'


_local = threading.local()
def sess():
    if not hasattr(_local, 's'):
        s = requests.Session(); s.headers.update(UA); _local.s = s
    return _local.s


def head(url, tries=3):
    for i in range(tries):
        try:
            r = sess().head(url, timeout=60, allow_redirects=True)
            if r.status_code == 200:
                cd = r.headers.get('content-disposition', '')
                fn = re.search(r'filename="?([^";]+)', cd)
                return {'size': int(r.headers.get('content-length', 0) or 0),
                        'content_type': r.headers.get('content-type', ''),
                        'filename': fn.group(1) if fn else ''}
            if r.status_code in (429, 500, 502, 503):
                time.sleep(2 * (i + 1)); continue
            return None
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    return None


def main():
    cands = {}
    for line in open(os.path.join(RAW, 'meetings.jsonl')):
        rec = json.loads(line)
        for a in rec['attachments']:
            k = kind_of(a['label'])
            if not k:
                continue
            fid = a['file_id']
            c = cands.setdefault(fid, {
                'file_id': fid, 'kind': k, 'label': a['label'], 'url': a['url'],
                'seen_at': [], 'label_date': date_in_label(a['label']),
                'stated': bool(STATED.search(a['label'])),
            })
            c['seen_at'].append({
                'meeting_id': rec['meeting_id'], 'date': rec.get('date'),
                'committee': rec.get('committee'), 'item': a.get('item_file'),
            })
    print(f'{len(cands)} candidate files', flush=True)

    cache_path = os.path.join(RAW, 'head_cache.json')
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    todo = [c for c in cands.values() if c['file_id'] not in cache]
    print(f'{len(cache)} cached, {len(todo)} to HEAD', flush=True)

    q = queue.Queue()
    for c in todo:
        q.put(c)
    lock = threading.Lock()
    n = [0]

    def worker():
        while True:
            try:
                c = q.get_nowait()
            except queue.Empty:
                return
            h = head(c['url'])
            with lock:
                cache[c['file_id']] = h or {}
                n[0] += 1
                if n[0] % 200 == 0:
                    print(f'  HEAD {n[0]}/{len(todo)}', flush=True)
                    json.dump(cache, open(cache_path, 'w'))

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for t in ts: t.start()
    for t in ts: t.join()
    json.dump(cache, open(cache_path, 'w'))

    # collapse: same kind + same hearing date + same byte size == same document
    groups = collections.defaultdict(list)
    unsized = 0
    for c in cands.values():
        h = cache.get(c['file_id']) or {}
        c['size'] = h.get('size', 0)
        c['filename'] = h.get('filename', '')
        c['content_type'] = h.get('content_type', '')
        if not c['size']:
            unsized += 1
            continue
        key = (c['kind'], c['label_date'] or 'nodate', c['size'])
        groups[key].append(c)

    out = open(os.path.join(RAW, 'documents.jsonl'), 'w')
    kept = 0
    for key, members in groups.items():
        members.sort(key=lambda c: int(c['file_id']))
        rep = members[0]
        rep['aliases'] = [m['file_id'] for m in members[1:]]
        for m in members[1:]:
            rep['seen_at'].extend(m['seen_at'])
        # dedupe seen_at
        seen, sa = set(), []
        for s in rep['seen_at']:
            k = (s['meeting_id'], s['item'])
            if k in seen:
                continue
            seen.add(k); sa.append(s)
        rep['seen_at'] = sa
        out.write(json.dumps(rep) + '\n')
        kept += 1
    out.close()

    total_bytes = sum(k[2] for k in groups)
    print(f'kept {kept} distinct documents ({total_bytes/1e9:.2f} GB), '
          f'{unsized} skipped for missing size', flush=True)
    by_kind = collections.Counter(k[0] for k in groups)
    for k, v in by_kind.most_common():
        print(f'   {k}: {v}', flush=True)

    if kept == 0:
        print('FAILED: no documents selected', flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
