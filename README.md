# What was said at the hearing

Full-text search across New York City Council hearing transcripts and written testimony.
A result comes back as an exchange — the question, and the answer — not a page number.

Live: (pending) · Source: [Legistar](https://legistar.council.nyc.gov/Calendar.aspx)

## The pipeline

Run in order. Every step resumes from where it stopped, so a killed run costs nothing.

```
python3 scripts/crawl_meetings.py    # meeting pages -> data/raw/meetings.jsonl
python3 scripts/select_documents.py  # pick + dedupe -> data/raw/documents.jsonl
python3 scripts/fetch_pdfs.py        # download, extract, discard the PDFs
python3 scripts/build_corpus.py      # group documents into hearings
python3 scripts/build_index.py       # write docs/data/
```

`data/pdf/` and `data/text/` are not in git: the PDFs are gigabytes and disposable,
and the extracted text is rebuilt from them. What ships is `docs/`.

## The one manual step

`data/raw/all-meetings.txt` is the list of Legistar meeting ids and GUIDs. Both are
needed — a meeting page will not open without its GUID.

Legistar's OData API now answers `403 Token is required`, and its calendar rejects
programmatic form posts, so the historical list was harvested once by driving the
calendar's year filter in a browser. **That is not needed again.** New meetings appear
on the calendar's default view, which plain `curl` can read:

```
curl -A 'Mozilla/5.0' https://legistar.council.nyc.gov/Calendar.aspx
```

Pull the `MeetingDetail.aspx?ID=…&GUID=…` links out of that, append any new ones to
`all-meetings.txt`, and re-run the pipeline. Only the backfill of 2024 and 2025 ever
needed a browser.

## Things that will bite you

- **A bill carries its whole document history.** The transcript of a January hearing is
  attached to every later meeting that touches the same bill. Never date a document by
  the meeting page you found it on; `build_corpus.py` dates transcripts from their own
  cover page.
- **The same file is uploaded many times over**, with a new file id each time. Dedupe on
  kind + date + byte size first, then on SHA-1 after download.
- **Transcripts extract as one word per line** with a page header and a stenographer
  footer. `extract_text.py` detects that shape and reflows it; ordinary prose is left
  with its paragraph breaks.
- **Appearance rosters lie in both directions** — they omit people who testified and
  list people who did not. They are deliberately not used.
- **Legistar needs a browser User-Agent.** Plain curl gets 403 on some paths.

## NotebookLM packs

There is no build step for these. `docs/packs.js` assembles a committee's Markdown in the
browser out of the same passage blocks the search already fetches. Writing them at build
time would put a quarter of a gigabyte of duplicated text in the repository to no purpose.
The results page has the other export: whatever the current search matched, with context
and Legistar links, as one file.

## Query language

Handled entirely in the browser by `docs/search.js`. `"phrase"` is literal and exact;
a bare word also matches its corpus-attested plural and tense variants, and falls back
to an edit-distance match when it occurs nowhere. `word*` is a prefix, `-word` excludes,
`word~` forces fuzzy, and `speaker:`, `role:`, `committee:`, `after:`, `before:` narrow
the field. `speaker:` and `role:` work because `build_index.py` writes them into the
index as reserved `zzspk_`/`zzrole_` terms, which the tokeniser can never produce.

Fuzzy and wildcard expansion run against `docs/data/dict.json`, the corpus vocabulary
with document frequencies. It loads lazily in the background; exact search never waits
for it.

## Layout

```
scripts/   the pipeline, in the order above
data/raw/  crawl output, committed (small); head_cache.json is not
site/      the static site — house.css, index.html, methodology.html, search.js
docs/data/ the built index: meta.json, hearings.json, idx/*.json, psg/*.json
```
