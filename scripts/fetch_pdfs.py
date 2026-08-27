#!/usr/bin/env python3
"""Download the selected documents, then extract and cache their text.

PDFs are large and disposable; they stay out of git. What we keep is the
extracted text plus a content hash, which is also how the last layer of
duplicates gets collapsed: Legistar hands out a fresh file id every time the
same document is uploaded against another bill.
"""
import hashlib, json, os, queue, subprocess, sys, threading, time, warnings
warnings.filterwarnings('ignore')
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
RAW = os.path.join(ROOT, 'data', 'raw')
PDF = os.path.join(ROOT, 'data', 'pdf')
TXT = os.path.join(ROOT, 'data', 'text')
os.makedirs(PDF, exist_ok=True)
os.makedirs(TXT, exist_ok=True)

MAX_BYTES = 60 * 1024 * 1024
# Appearance cards are scanned sign-in sheets: no extractable text, and over a
# gigabyte of it. They stay in documents.jsonl as part of the record but are
# not downloaded. Raise TESTIMONY_CAP once you have measured the text yield.
SKIP_KINDS = {'appearance'}
TESTIMONY_CAP = int(os.environ.get('TESTIMONY_CAP', 60 * 1024 * 1024))
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}

EXTRACT_TIMEOUT = int(os.environ.get('EXTRACT_TIMEOUT', 180))


def extract_pages(path):
    """Extract in a subprocess so a malformed PDF can be killed.

    pypdf spins for ever on a handful of files in this corpus. In-process that
    wedges a worker thread and, with every thread eventually stuck on one, the
    whole run silently stops making progress.
    """
    r = subprocess.run([sys.executable, os.path.join(HERE, 'extract_text.py'), path, '--json'],
                       capture_output=True, timeout=EXTRACT_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b'')[-200:].decode('utf-8', 'replace'))
    return json.loads(r.stdout)

_local = threading.local()
def sess():
    if not hasattr(_local, 's'):
        s = requests.Session(); s.headers.update(UA); _local.s = s
    return _local.s


def fetch(url, dest, tries=3):
    for i in range(tries):
        try:
            with sess().get(url, timeout=300, stream=True) as r:
                if r.status_code != 200:
                    if r.status_code in (429, 500, 502, 503):
                        time.sleep(3 * (i + 1)); continue
                    return None
                h = hashlib.sha1()
                n = 0
                with open(dest, 'wb') as f:
                    for chunk in r.iter_content(1 << 16):
                        n += len(chunk)
                        if n > MAX_BYTES:
                            f.close(); os.remove(dest)
                            return 'TOOBIG'
                        h.update(chunk); f.write(chunk)
                return h.hexdigest()
        except requests.RequestException:
            time.sleep(3 * (i + 1))
    return None


def main():
    docs = [json.loads(l) for l in open(os.path.join(RAW, 'documents.jsonl'))]
    state_path = os.path.join(RAW, 'fetch_state.json')
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}

    skipped = 0
    keep = []
    for d in docs:
        if d['kind'] in SKIP_KINDS:
            skipped += 1
            continue
        if d['kind'] == 'testimony' and d.get('size', 0) > TESTIMONY_CAP:
            skipped += 1
            continue
        keep.append(d)

    # Transcripts first: they are the question-and-answer record, they are
    # text-native, and all of them together weigh less than a tenth of the
    # testimony. Within a kind, smallest first, so the cheap text lands early.
    order = {'transcript': 0, 'testimony': 1}
    keep.sort(key=lambda d: (order.get(d['kind'], 2), d.get('size', 0)))

    todo = [d for d in keep if d['file_id'] not in state]
    gb = sum(d.get('size', 0) for d in todo) / 1e9
    print(f'{len(docs)} documents, {skipped} skipped by kind or size, '
          f'{len(state)} done, {len(todo)} to fetch ({gb:.2f} GB)', flush=True)

    q = queue.Queue()
    for d in todo:
        q.put(d)
    lock = threading.Lock()
    n = {'ok': 0, 'fail': 0, 'big': 0, 'dup': 0, 'chars': 0, 'bytes': 0}

    def worker():
        while True:
            try:
                d = q.get_nowait()
            except queue.Empty:
                return
            fid = d['file_id']
            dest = os.path.join(PDF, f'{fid}.pdf')
            digest = fetch(d['url'], dest)
            rec = {'file_id': fid}
            if digest == 'TOOBIG':
                rec['status'] = 'too_big'
                with lock:
                    state[fid] = rec; n['big'] += 1
                continue
            if not digest:
                rec['status'] = 'fetch_failed'
                with lock:
                    state[fid] = rec; n['fail'] += 1
                continue
            rec['sha1'] = digest
            rec['size'] = d.get('size', 0)
            rec['kind'] = d['kind']
            try:
                pages = extract_pages(dest)
            except subprocess.TimeoutExpired:
                rec['status'] = 'extract_timeout'
                try:
                    os.remove(dest)
                except OSError:
                    pass
                with lock:
                    state[fid] = rec; n['fail'] += 1
                continue
            except Exception as e:
                rec['status'] = 'extract_failed'
                rec['error'] = str(e)[:200]
                try:
                    os.remove(dest)
                except OSError:
                    pass
                with lock:
                    state[fid] = rec; n['fail'] += 1
                continue
            text = '\n\n'.join(pages)
            rec['pages'] = len(pages)
            rec['chars'] = len(text)
            rec['status'] = 'ok' if len(text) > 400 else 'no_text'
            with open(os.path.join(TXT, f'{fid}.txt'), 'w') as f:
                f.write(text)
            with open(os.path.join(TXT, f'{fid}.pages.json'), 'w') as f:
                json.dump([len(p) for p in pages], f)
            os.remove(dest)
            with lock:
                state[fid] = rec
                n['ok' if rec['status'] == 'ok' else 'fail'] += 1
                n['chars'] += rec['chars']
                n['bytes'] += rec.get('size', 0)
                tot = n['ok'] + n['fail'] + n['big']
                if tot % 25 == 0:
                    print(f"  {tot}/{len(todo)} ok={n['ok']} fail={n['fail']} "
                          f"big={n['big']} {n['chars']/1e6:.0f}M chars "
                          f"{n['bytes']/1e9:.2f}GB", flush=True)
                    json.dump(state, open(state_path, 'w'))

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()
    json.dump(state, open(state_path, 'w'))
    print(f"DONE ok={n['ok']} fail={n['fail']} too_big={n['big']} "
          f"{n['chars']/1e6:.1f}M chars", flush=True)

    ok = sum(1 for v in state.values() if v.get('status') == 'ok')
    if ok == 0:
        print('FAILED: nothing extracted', flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
