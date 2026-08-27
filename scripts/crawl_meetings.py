#!/usr/bin/env python3
"""Walk Legistar meeting pages and record every attachment worth ingesting.

Input:  data/raw/all-meetings.txt  ("<meetingId>:<guid-no-dashes>" per line)
Output: data/raw/meetings.jsonl    (one record per meeting, with attachments)

Legistar's OData API now demands a token (403 "Token is required"), so this
scrapes InSite. GETs are fine; only form postbacks are blocked, and those are
needed solely for the historical calendar backfill, which is already done.
"""
import json, os, re, sys, time, threading, queue, warnings
warnings.filterwarnings('ignore')
import requests
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, '..', 'data', 'raw')
BASE = 'https://legistar.council.nyc.gov/'
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}

def dash(g):
    return f'{g[0:8]}-{g[8:12]}-{g[12:16]}-{g[16:20]}-{g[20:32]}'

_local = threading.local()
def sess():
    if not hasattr(_local, 's'):
        s = requests.Session(); s.headers.update(UA)
        _local.s = s
    return _local.s

def get(url, tries=4):
    for i in range(tries):
        try:
            r = sess().get(url, timeout=90)
            if r.status_code == 200:
                return r.text
            if r.status_code in (429, 500, 502, 503):
                time.sleep(2 * (i + 1)); continue
            return None
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    return None

def plain(html):
    t = re.sub(r'<script.*?</script>', ' ', html, flags=re.S)
    t = re.sub(r'<style.*?</style>', ' ', t, flags=re.S)
    t = re.sub(r'<[^>]+>', ' ', t)
    import html as H
    return H.unescape(re.sub(r'\s+', ' ', t))

FIELD = re.compile(
    r'Meeting Name:\s*(?P<name>.*?)\s*Agenda status:\s*(?P<astatus>.*?)\s*'
    r'Meeting date/time:\s*(?P<when>.*?)\s*Minutes status:\s*(?P<mstatus>.*?)\s*'
    r'Meeting location:\s*(?P<loc>.*?)\s*(?:Published agenda|Attachments|Meeting Items)')

def parse_meeting(mid, guid):
    url = f'{BASE}MeetingDetail.aspx?ID={mid}&GUID={dash(guid)}&Options=info|&Search='
    html = get(url)
    if not html or 'Meeting Name' not in html:
        return None
    txt = plain(html)
    m = FIELD.search(txt)
    rec = {'meeting_id': mid, 'guid': dash(guid), 'url': url}
    if m:
        d = m.groupdict()
        rec['committee'] = d['name'].strip()
        when = d['when'].strip()
        rec['datetime'] = when
        dm = re.match(r'(\d{1,2}/\d{1,2}/\d{4})\s*(.*)', when)
        if dm:
            mm, dd, yy = dm.group(1).split('/')
            rec['date'] = f'{yy}-{int(mm):02d}-{int(dd):02d}'
            rec['time'] = dm.group(2).strip()
        rec['location'] = d['loc'].strip()
        rec['agenda_status'] = d['astatus'].strip()
        rec['minutes_status'] = d['mstatus'].strip()

    soup = BeautifulSoup(html, 'html.parser')
    atts, items = [], []
    for a in soup.find_all('a', href=True):
        h = a['href']
        label = a.get_text(strip=True)
        if 'View.ashx' in h:
            mode = re.search(r'M=([A-Z]+)', h)
            atts.append({'label': label, 'href': h, 'mode': mode.group(1) if mode else None,
                         'level': 'meeting'})
        elif 'LegislationDetail.aspx' in h:
            im = re.search(r'ID=(\d+)&GUID=([A-Z0-9-]+)', h, re.I)
            if im:
                items.append({'file': label, 'id': im.group(1), 'guid': im.group(2), 'href': h})

    # dedupe agenda items, then pull each item's own attachments
    seen, uniq = set(), []
    for it in items:
        if it['id'] in seen:
            continue
        seen.add(it['id']); uniq.append(it)

    for it in uniq:
        ihtml = get(BASE + it['href'])
        if not ihtml:
            continue
        isoup = BeautifulSoup(ihtml, 'html.parser')
        itxt = plain(ihtml)
        tm = re.search(r'Name:\s*(.*?)\s*(?:Status|Type|File created)', itxt)
        it['title'] = tm.group(1).strip()[:400] if tm else ''
        for a in isoup.find_all('a', href=True):
            if 'View.ashx?M=F' in a['href']:
                atts.append({'label': a.get_text(strip=True), 'href': a['href'],
                             'mode': 'F', 'level': 'item', 'item_file': it['file'],
                             'item_id': it['id']})
        it.pop('href', None)

    # dedupe attachments by file id
    seen, ua = set(), []
    for at in atts:
        fid = re.search(r'ID=(\d+)', at['href'])
        key = fid.group(1) if fid else at['href']
        if key in seen:
            continue
        seen.add(key)
        at['file_id'] = key
        at['url'] = BASE + at['href'].replace('&amp;', '&')
        at.pop('href', None)
        ua.append(at)

    rec['items'] = uniq
    rec['attachments'] = ua
    return rec


def main():
    pairs = []
    with open(os.path.join(RAW, 'all-meetings.txt')) as f:
        for line in f:
            line = line.strip()
            if ':' in line:
                mid, guid = line.split(':')
                pairs.append((mid, guid))

    out_path = os.path.join(RAW, 'meetings.jsonl')
    done = set()
    if os.path.exists(out_path):
        with open(out_path) as f:
            for line in f:
                try:
                    done.add(json.loads(line)['meeting_id'])
                except Exception:
                    pass
    todo = [p for p in pairs if p[0] not in done]
    print(f'{len(pairs)} meetings, {len(done)} already crawled, {len(todo)} to go', flush=True)

    q = queue.Queue()
    for p in todo:
        q.put(p)
    lock = threading.Lock()
    out = open(out_path, 'a')
    counter = {'n': 0, 'fail': 0}

    def worker():
        while True:
            try:
                mid, guid = q.get_nowait()
            except queue.Empty:
                return
            try:
                rec = parse_meeting(mid, guid)
            except Exception as e:
                rec = None
                with lock:
                    print(f'  ERROR {mid}: {e}', flush=True)
            with lock:
                if rec:
                    out.write(json.dumps(rec) + '\n'); out.flush()
                    counter['n'] += 1
                else:
                    counter['fail'] += 1
                tot = counter['n'] + counter['fail']
                if tot % 25 == 0:
                    print(f'  {tot}/{len(todo)}  ok={counter["n"]} fail={counter["fail"]}', flush=True)
            q.task_done()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out.close()
    print(f'DONE ok={counter["n"]} fail={counter["fail"]}', flush=True)

    if counter['fail'] > len(todo) * 0.1 and len(todo) > 20:
        print(f'FAILED: {counter["fail"]} of {len(todo)} meetings did not parse — '
              f'check whether Legistar changed or is throttling', flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
