/* Client-side search over the New York City Council hearing corpus.
   The index is a set of static JSON shards; nothing here talks to a server. */

const DATA = 'data/';
const state = {
  meta: null, hearings: null,
  shards: new Map(), blocks: new Map(),
  q: '', exact: false, parsed: null, expansion: [], missing: [],
  filters: { role: new Set(), committee: '', from: '', to: '', kind: '' },
  results: [], shown: 0, seq: 0,
};

const PAGE = 20;
const STOP = new Set(`a an and are as at be been but by for from had has have he her his i if in
into is it its of on or she that the their them they this to was were will with you your
we our us not no so do does did been being at over under about after before between
than then there here what which who whom whose when where why how all any both each few
more most other some such only own same too very can just should now`.split(/\s+/));

const $ = s => document.querySelector(s);
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; };

/* ---------- tokenising, matched to the builder ---------- */
const TOKEN = /[a-z0-9][a-z0-9''\-.]*/g;
function tokenize(s) {
  const out = [];
  const t = s.normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase();
  let m;
  TOKEN.lastIndex = 0;
  while ((m = TOKEN.exec(t))) {
    const w = m[0].replace(/^[.\-']+|[.\-']+$/g, '');
    if (w) out.push(w);
  }
  return out;
}

/* ---------- the query language ----------
   "phrase"      the words in that order, literally; no fuzzy matching inside
   word          a word, with its close variants unless exact mode is on
   word*         any word starting with that
   -word         passages that do not contain it
   speaker:tisch limited to a speaker whose label contains that word
   role:council  council | administration | public | written | staff
   committee:fin hearings whose committee name contains that
   after:2025-01-01 / before:2026-06-30
   Bare words are all required. OR between two words makes either enough. */

const FIELD = /^(speaker|role|committee|after|before|since|until)\s*:\s*(.+)$/i;
const ROLES = ['council', 'administration', 'public', 'written', 'staff', 'unidentified'];

function parseQuery(q) {
  const clauses = [], fields = { speaker: [], role: [], committee: [], after: '', before: '' };
  const re = /(-)?"([^"]*)"|(-)?(\S+)/g;
  let m;
  const raw = [];
  while ((m = re.exec(q))) {
    const neg = !!(m[1] || m[3]);
    if (m[2] != null) raw.push({ neg, phrase: true, text: m[2] });
    else raw.push({ neg, phrase: false, text: m[4] });
  }

  for (let i = 0; i < raw.length; i++) {
    const r = raw[i];
    if (!r.phrase) {
      const f = FIELD.exec(r.text);
      if (f) {
        const key = f[1].toLowerCase(), val = f[2];
        if (key === 'speaker') fields.speaker.push(...tokenize(val));
        else if (key === 'role') { const v = val.toLowerCase(); if (ROLES.includes(v)) fields.role.push(v); }
        else if (key === 'committee') fields.committee.push(val.toLowerCase());
        else if (key === 'after' || key === 'since') fields.after = val;
        else if (key === 'before' || key === 'until') fields.before = val;
        continue;
      }
      if (/^or$/i.test(r.text)) { raw[i].isOr = true; continue; }
    }
    if (r.phrase) {
      const toks = tokenize(r.text);
      const seq = [];
      toks.forEach((w, k) => { if (!STOP.has(w)) seq.push({ term: w, off: k }); });
      if (seq.length) clauses.push({ kind: 'phrase', neg: r.neg, seq, text: r.text });
      continue;
    }
    let word = r.text;
    let prefix = false, fuzzy = false;
    if (word.endsWith('*')) { prefix = true; word = word.slice(0, -1); }
    if (word.endsWith('~')) { fuzzy = true; word = word.slice(0, -1); }
    const toks = tokenize(word).filter(w => !STOP.has(w));
    if (!toks.length) continue;
    toks.forEach(t => clauses.push({ kind: 'term', neg: r.neg, term: t, prefix, fuzzy, text: r.text }));
  }

  // "a OR b": fold the clause after an OR into the one before it
  const folded = [];
  for (let i = 0; i < raw.length; i++) { /* markers only */ }
  const orAt = [];
  q.replace(/\S+/g, (w, idx) => { if (/^or$/i.test(w)) orAt.push(idx); return w; });
  return { clauses, fields, orCount: orAt.length };
}

/* ---------- vocabulary, for wildcard and fuzzy expansion ---------- */
let dictPromise = null;
function getDict() {
  if (!dictPromise) {
    dictPromise = fetch(DATA + 'dict.json').then(r => r.ok ? r.json() : [])
      .then(v => ({ list: v, map: new Map(v.map(([t, df]) => [t, df])) }))
      .catch(() => ({ list: [], map: new Map() }));
  }
  return dictPromise;
}

/* Levenshtein with an early exit once the distance passes the budget. */
function editDistance(a, b, max) {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  const prev = new Array(b.length + 1), cur = new Array(b.length + 1);
  for (let j = 0; j <= b.length; j++) prev[j] = j;
  for (let i = 1; i <= a.length; i++) {
    cur[0] = i;
    let best = cur[0];
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1,
                        prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      if (cur[j] < best) best = cur[j];
    }
    if (best > max) return max + 1;
    for (let j = 0; j <= b.length; j++) prev[j] = cur[j];
  }
  return prev[b.length];
}

/* Plural and tense variants, checked against the vocabulary rather than
   stemmed: a real stemmer turns "housing" into "hous" and surprises people. */
function variantsOf(w) {
  const v = new Set();
  const add = x => { if (x && x !== w && x.length > 2) v.add(x); };
  add(w + 's');
  // "rat" + "es" is "rates", a different word entirely. Only take -es where
  // English actually forms the plural that way, and leave tense endings alone
  // on short words, where they collide with unrelated vocabulary.
  if (/(s|x|z|ch|sh)$/.test(w)) add(w + 'es');
  if (w.endsWith('y') && w.length > 3) add(w.slice(0, -1) + 'ies');
  if (w.endsWith('s') && w.length > 3) add(w.slice(0, -1));
  if (w.endsWith('es') && w.length > 4) add(w.slice(0, -2));
  if (w.endsWith('ies') && w.length > 4) add(w.slice(0, -3) + 'y');
  if (w.length >= 5) {
    add(w + 'ed'); add(w + 'ing'); add(w + 'd');
    if (w.endsWith('e')) add(w.slice(0, -1) + 'ing');
    // Deliberately no -ing reduction: in this corpus "housing" is not "house",
    // "hearing" is not "hear" and "building" is not "build". Stripping -ed is
    // safe by comparison ("evicted" is "evict").
    if (w.endsWith('ed')) { add(w.slice(0, -2)); add(w.slice(0, -1)); }
  }
  return [...v];
}

const MAX_PREFIX = 60, MAX_FUZZY = 8;

async function expandTerm(c) {
  const { list, map } = await getDict();
  const w = c.term;
  const alts = [];
  const seen = new Set();
  const push = (t, weight, how) => {
    if (seen.has(t)) return;
    seen.add(t); alts.push({ term: t, weight, how });
  };

  if (c.prefix) {
    const hits = list.filter(([t]) => t.startsWith(w));
    hits.sort((a, b) => b[1] - a[1]);
    hits.slice(0, MAX_PREFIX).forEach(([t]) => push(t, t === w ? 1 : 0.85, 'starts with'));
    if (!alts.length) push(w, 1, 'exact');
    return alts;
  }

  push(w, 1, 'exact');
  if (state.exact) return alts;

  variantsOf(w).forEach(t => { if (map.has(t)) push(t, 0.82, 'variant'); });

  const wantFuzzy = c.fuzzy || !map.has(w);
  if (wantFuzzy && w.length >= 4) {
    const budget = w.length >= 7 ? 2 : 1;
    const near = [];
    for (const [t, df] of list) {
      if (Math.abs(t.length - w.length) > budget) continue;
      if (seen.has(t)) continue;
      const d = editDistance(w, t, budget);
      if (d <= budget) near.push([t, d, df]);
    }
    near.sort((a, b) => a[1] - b[1] || b[2] - a[2]);
    near.slice(0, MAX_FUZZY).forEach(([t, d]) =>
      push(t, d === 1 ? 0.62 : 0.42, 'close to'));
  }
  return alts;
}

function shardOf(term) {
  let h = 2166136261;
  for (let i = 0; i < term.length; i++) { h ^= term.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
  return h % state.meta.shards;
}

async function getShard(n) {
  if (state.shards.has(n)) return state.shards.get(n);
  const p = fetch(`${DATA}idx/${n}.json`).then(r => r.ok ? r.json() : {});
  state.shards.set(n, p);
  return p;
}

/* postings: [df, hasPositions, then per passage: dPid, weight,
   and when positions are kept, tf followed by tf delta-encoded positions] */
function decode(flat) {
  const out = new Map();
  const hasPos = flat[1] === 1;
  let i = 2, pid = 0;
  while (i < flat.length) {
    pid += flat[i++];
    const w = flat[i++];
    let pos = null;
    if (hasPos) {
      const tf = flat[i++];
      pos = new Array(tf);
      let p = 0;
      for (let k = 0; k < tf; k++) { p += flat[i++]; pos[k] = p; }
    }
    out.set(pid, { w, pos });
  }
  return { df: flat[0], hasPos, posting: out };
}

async function lookup(term) {
  const sh = await getShard(shardOf(term));
  const flat = sh[term];
  if (!flat) return null;
  if (!flat.__d) Object.defineProperty(flat, '__d', { value: decode(flat), enumerable: false });
  return flat.__d;
}

/* ---------- search ---------- */
async function postingsFor(term) {
  const d = await lookup(term);
  return d;
}

/* A clause is satisfied by any one of its alternatives. Its cost, for choosing
   which clause to scan first, is the sum of its alternatives' document
   frequencies. */
async function buildClause(c) {
  if (c.kind === 'phrase') {
    const parts = [];
    for (const t of c.seq) {
      const d = await postingsFor(t.term);
      if (!d) return null;                       // a word nobody ever said
      parts.push({ ...d, off: t.off, term: t.term });
    }
    const verifiable = parts.length > 1 && parts.every(p => p.hasPos);
    if (parts.length > 1 && !verifiable) state.loosePhrase = true;
    return { kind: 'phrase', neg: c.neg, parts, verifiable,
             cost: Math.min(...parts.map(p => p.df)), text: c.text };
  }
  const alts = await expandTerm(c);
  const live = [];
  for (const a of alts) {
    const d = await postingsFor(a.term);
    if (d) live.push({ ...d, term: a.term, weight: a.weight, how: a.how });
  }
  if (!live.length) return null;
  return { kind: 'term', neg: c.neg, alts: live, term: c.term,
           cost: live.reduce((n, a) => n + a.df, 0), text: c.text };
}

function clauseHas(cl, pid) {
  if (cl.kind === 'phrase') {
    if (!cl.verifiable) return cl.parts.every(p => p.posting.has(pid));
    if (!cl.parts.every(p => p.posting.has(pid))) return false;
    const base = cl.parts[0];
    return base.posting.get(pid).pos.some(p0 =>
      cl.parts.every(part => part.posting.get(pid).pos.includes(p0 - base.off + part.off)));
  }
  return cl.alts.some(a => a.posting.has(pid));
}

async function runSearch(q) {
  const parsed = parseQuery(q);
  state.parsed = parsed;
  state.loosePhrase = false;
  state.expansion = [];
  state.missing = [];
  if (!parsed.clauses.length && !parsed.fields.speaker.length && !parsed.fields.role.length) {
    return [];
  }
  const N = state.meta.passages;
  const idf = df => Math.log(1 + (N - df + 0.5) / (df + 0.5));

  const positive = [], negative = [];
  for (const c of parsed.clauses) {
    const cl = await buildClause(c);
    if (!cl) {
      if (!c.neg) { state.missing.push(c.text); return []; }
      continue;
    }
    (cl.neg ? negative : positive).push(cl);
  }

  // speaker: and role: ride the same machinery, as reserved index terms
  for (const w of parsed.fields.speaker) {
    const d = await postingsFor('zzspk_' + w);
    if (!d) { state.missing.push('speaker:' + w); return []; }
    positive.push({ kind: 'term', alts: [{ ...d, term: w, weight: 0, how: 'speaker' }],
                    cost: d.df, term: w, field: true });
  }
  if (parsed.fields.role.length) {
    const alts = [];
    for (const r of parsed.fields.role) {
      const d = await postingsFor('zzrole_' + r);
      if (d) alts.push({ ...d, term: r, weight: 0, how: 'role' });
    }
    if (!alts.length) return [];
    positive.push({ kind: 'term', alts, cost: alts.reduce((n, a) => n + a.df, 0), field: true });
  }

  if (!positive.length) return [];

  for (const cl of positive) {
    if (cl.kind === 'term' && !cl.field) {
      state.expansion.push({
        term: cl.term,
        alts: cl.alts.filter(a => a.term !== cl.term).map(a => ({ t: a.term, how: a.how })),
      });
    }
  }

  positive.sort((a, b) => a.cost - b.cost);
  const seed = positive[0];
  let candidates;
  if (seed.kind === 'phrase') {
    candidates = [...seed.parts.reduce((best, p) => !best || p.df < best.df ? p : best, null)
      .posting.keys()].filter(pid => clauseHas(seed, pid));
  } else {
    const set = new Set();
    seed.alts.forEach(a => a.posting.forEach((_, pid) => set.add(pid)));
    candidates = [...set];
  }
  for (let i = 1; i < positive.length && candidates.length; i++) {
    candidates = candidates.filter(pid => clauseHas(positive[i], pid));
  }
  for (const cl of negative) {
    candidates = candidates.filter(pid => !clauseHas(cl, pid));
  }

  const hits = [];
  for (const pid of candidates) {
    let score = 0;
    for (const cl of positive) {
      if (cl.field) continue;
      if (cl.kind === 'phrase') {
        for (const p of cl.parts) {
          const e = p.posting.get(pid);
          if (e) score += idf(p.df) * (e.w / 255) * 1.6;   // a phrase hit counts for more
        }
      } else {
        let best = 0;
        for (const a of cl.alts) {
          const e = a.posting.get(pid);
          if (e) best = Math.max(best, idf(a.df) * (e.w / 255) * a.weight);
        }
        score += best;
      }
    }
    hits.push({ pid, score });
  }
  hits.sort((a, b) => b.score - a.score);
  return hits;
}

/* When nothing matched, the nearest words that would have. */
async function didYouMean(term) {
  const { list, map } = await getDict();
  if (map.has(term) || term.length < 4) return [];
  // more generous than the automatic expansion: by the time we are here,
  // nothing matched, so a looser guess is better than an empty page
  const budget = Math.min(3, Math.max(1, Math.floor(term.length / 3)));
  const near = [];
  for (const [t, df] of list) {
    if (Math.abs(t.length - term.length) > budget) continue;
    const d = editDistance(term, t, budget);
    if (d <= budget) near.push([t, d, df]);
  }
  near.sort((a, b) => a[1] - b[1] || b[2] - a[2]);
  return near.slice(0, 5).map(x => x[0]);
}

/* ---------- passage text ---------- */
async function getBlock(n) {
  if (state.blocks.has(n)) return state.blocks.get(n);
  const p = fetch(`${DATA}psg/${n}.json`).then(r => r.ok ? r.json() : []);
  state.blocks.set(n, p);
  return p;
}
async function passage(pid) {
  if (pid < 0 || pid >= state.meta.passages) return null;
  const b = Math.floor(pid / state.meta.block);
  const blk = await getBlock(b);
  const row = blk.p && blk.p[pid - b * state.meta.block];
  if (!row) return null;
  return { s: blk.sp[row[0]], r: blk.ro[row[1]], t: row[2], pid,
           h: hearingOf(pid) };
}

/* ---------- filters ----------
   Passage ids are allocated hearing by hearing, so a passage's hearing is a
   binary search over the ranges in hearings.json -- no text fetch needed. */
function hearingOf(pid) {
  const hs = state.hearings;
  let lo = 0, hi = hs.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1, h = hs[mid];
    if (pid < h.first) hi = mid - 1;
    else if (pid >= h.first + h.n) lo = mid + 1;
    else return mid;
  }
  return -1;
}

function hearingPasses(h) {
  const f = state.filters;
  if (f.committee && !h.committees.includes(f.committee)) return false;
  if (f.from && h.date < f.from) return false;
  if (f.to && h.date > f.to) return false;
  if (f.kind === 'committee' && h.stated) return false;
  if (f.kind === 'stated' && !h.stated) return false;
  // committee:, after: and before: typed into the query box narrow it further
  const qf = (state.parsed && state.parsed.fields) || null;
  if (qf) {
    if (qf.after && h.date < qf.after) return false;
    if (qf.before && h.date > qf.before) return false;
    if (qf.committee.length) {
      const names = h.committees.join(' · ').toLowerCase();
      if (!qf.committee.every(c => names.includes(c))) return false;
    }
  }
  return true;
}

/* The set of hearings a filter admits, as passage-id ranges. */
function allowedRanges() {
  const f = state.filters;
  const qf = (state.parsed && state.parsed.fields) || null;
  const queryScoped = qf && (qf.after || qf.before || qf.committee.length);
  if (!f.committee && !f.from && !f.to && !f.kind && !queryScoped) return null;
  return state.hearings.filter(hearingPasses).map(h => [h.first, h.first + h.n]);
}

function inRanges(ranges, pid) {
  let lo = 0, hi = ranges.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1, r = ranges[mid];
    if (pid < r[0]) hi = mid - 1;
    else if (pid >= r[1]) lo = mid + 1;
    else return true;
  }
  return false;
}

async function roleSet() {
  const f = state.filters;
  if (!f.role.size) return null;
  const sets = [];
  for (const r of f.role) {
    const d = await lookup('zzrole_' + r);
    if (d) sets.push(d.posting);
  }
  if (!sets.length) return new Set();
  return sets;   // union: a pid passes if any set has it
}

/* ---------- rendering ---------- */
function highlight(text, terms) {
  if (!terms.length) return document.createTextNode(text);
  const frag = document.createDocumentFragment();
  const re = new RegExp('\\b(' + terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'gi');
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
    frag.appendChild(el('mark', null, m[0]));
    last = m.index + m[0].length;
  }
  frag.appendChild(document.createTextNode(text.slice(last)));
  return frag;
}

function trimAround(text, terms, want = 460) {
  if (text.length <= want) return { text, cut: false };
  if (!terms.length) {
    const e0 = text.lastIndexOf(' ', want);
    return { text: text.slice(0, e0 > 0 ? e0 : want) + ' …', cut: true };
  }
  const re = new RegExp('\\b(' + terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'i');
  const m = re.exec(text);
  const at = m ? m.index : 0;
  let s = Math.max(0, at - Math.floor(want / 3));
  if (s > 0) { const sp = text.indexOf(' ', s); if (sp > -1 && sp - s < 30) s = sp + 1; }
  let e = Math.min(text.length, s + want);
  const sp2 = text.lastIndexOf(' ', e);
  if (sp2 > s + want * 0.6) e = sp2;
  return { text: (s ? '… ' : '') + text.slice(s, e) + (e < text.length ? ' …' : ''), cut: true };
}

function speakerLine(p) {
  const name = /[a-z]/.test(p.s) ? p.s : titleish(p.s);
  const roleWord = { council: 'Council', administration: 'Administration',
                     public: 'Public witness', written: 'Written testimony',
                     staff: 'Committee staff', unidentified: 'Unidentified',
                     other: '' }[p.r] || '';
  return { name, roleWord };
}

function titleish(s) {
  return s.replace(/\b([A-Z])([A-Z'’\-]+)\b/g, (_, a, b) => a + b.toLowerCase())
          .replace(/\bMr\b/, 'Mr.').replace(/\bMs\b/, 'Ms.');
}

function fmtDate(d) {
  const [y, m, dd] = d.split('-').map(Number);
  const M = ['Jan.', 'Feb.', 'March', 'April', 'May', 'June', 'July', 'Aug.',
             'Sept.', 'Oct.', 'Nov.', 'Dec.'][m - 1];
  return `${M} ${dd}, ${y}`;
}

/* ---------- result list ---------- */
let queryTerms = [];

async function render(reset) {
  const list = $('#results');
  if (reset) { list.innerHTML = ''; state.shown = 0; }

  const hits = state.results;
  if (!hits.length) {
    list.innerHTML = '';
    list.appendChild(emptyNote());
    $('#count').textContent = state.q ? 'No matches' : '';
    $('#export').hidden = true;
    renderSuggestions();
    return;
  }
  $('#suggest').hidden = true;

  const slice = hits.slice(state.shown, state.shown + PAGE);
  state.shown += slice.length;

  // only the blocks this page needs, plus the neighbours used for context
  const need = new Set();
  slice.forEach(h => {
    for (const pid of [h.pid - 1, h.pid, h.pid + 1]) {
      if (pid >= 0 && pid < state.meta.passages) need.add(Math.floor(pid / state.meta.block));
    }
  });
  await Promise.all([...need].map(getBlock));

  let lastH = list.dataset.lastH || null;
  for (const hit of slice) {
    const p = await passage(hit.pid);
    if (!p) continue;
    const h = state.hearings[hit.hi];
    if (h.id !== lastH) {
      list.appendChild(hearingHead(h));
      lastH = h.id;
    }
    list.appendChild(await exchangeBlock(hit, p, h));
  }
  list.dataset.lastH = lastH;

  $('#more').hidden = state.shown >= hits.length;
  $('#more').textContent = `Show ${Math.min(PAGE, hits.length - state.shown)} more`;
  const nh = new Set(hits.map(x => x.h)).size;
  $('#count').textContent =
    `${hits.length.toLocaleString()} ${hits.length === 1 ? 'passage' : 'passages'} in ` +
    `${nh.toLocaleString()} ${nh === 1 ? 'hearing' : 'hearings'}` +
    (state.loosePhrase ? ' · matched as words, not as a phrase' : '');
  $('#export').hidden = false;
}

function hearingHead(h) {
  const d = el('div', 'hhead');
  const a = el('div', 'hh-left');
  a.appendChild(el('b', null, h.committees.join(' · ')));
  const meta = el('span', 'meta');
  meta.textContent = fmtDate(h.date) + (h.stated ? ' · Stated meeting' : '');
  a.appendChild(meta);
  d.appendChild(a);
  const links = el('div', 'hh-right');
  h.docs.slice(0, 4).forEach(doc => {
    const l = el('a', 'doclink', doc.k === 'transcript' ? 'Transcript' : 'Testimony');
    l.href = doc.u; l.target = '_blank'; l.rel = 'noopener';
    l.title = doc.l + (doc.p ? ` · ${doc.p} pages` : '');
    links.appendChild(l);
  });
  d.appendChild(links);
  return d;
}

async function exchangeBlock(hit, p, h) {
  const wrap = el('div', 'xch');
  const before = await passage(hit.pid - 1);
  const after = await passage(hit.pid + 1);

  if (before && before.h === p.h) wrap.appendChild(turnRow(before, false));
  wrap.appendChild(turnRow(p, true));
  if (after && after.h === p.h) wrap.appendChild(turnRow(after, false));

  const foot = el('div', 'xfoot');
  const open = el('button', 'chip', 'Read more of this exchange');
  open.onclick = () => openDrawer(hit.pid, h);
  foot.appendChild(open);
  wrap.appendChild(foot);
  return wrap;
}

function turnRow(p, isHit) {
  const row = el('div', 'turn' + (isHit ? ' hit' : ''));
  const { name, roleWord } = speakerLine(p);
  const sp = el('div', 'sp');
  sp.appendChild(el('b', null, name));
  if (roleWord) sp.appendChild(el('i', 'role r-' + p.r, roleWord));
  row.appendChild(sp);

  const body = el('div', 'txt');
  if (isHit) {
    const { text } = trimAround(p.t, queryTerms);
    body.appendChild(highlight(text, queryTerms));
  } else {
    const t = p.t.length > 260 ? p.t.slice(0, 260).replace(/\s\S*$/, '') + ' …' : p.t;
    body.textContent = t;
  }
  row.appendChild(body);
  return row;
}

function emptyNote() {
  const d = el('div', 'empty');
  if (!state.q) {
    d.textContent = 'Search the transcripts and written testimony of New York City Council hearings.';
  } else {
    d.innerHTML = '';
    d.appendChild(el('p', null, `Nothing matches ${JSON.stringify(state.q)} with the filters you have set.`));
    d.appendChild(el('p', null, 'Try fewer words, drop the quotation marks, or clear the filters.'));
  }
  return d;
}

/* ---------- drawer: the full run of an exchange ---------- */
async function openDrawer(pid, h) {
  const dr = $('#drawer');
  dr.classList.add('open');
  $('#dtitle').textContent = h.committees.join(' · ');
  $('#dlbl').textContent = fmtDate(h.date) + (h.stated ? ' · Stated meeting' : '');
  const body = $('#dbody');
  body.innerHTML = '';

  const docs = el('div', 'dlinks');
  h.docs.forEach(doc => {
    const a = el('a', 'srcbtn', (doc.k === 'transcript' ? 'Transcript' : 'Written testimony') +
      (doc.p ? ` · ${doc.p} pp.` : ''));
    a.href = doc.u; a.target = '_blank'; a.rel = 'noopener';
    docs.appendChild(a);
  });
  body.appendChild(docs);

  const runWrap = el('div', 'run');
  body.appendChild(runWrap);
  const atStart = pid === h.first;
  for (let i = atStart ? pid : pid - 6; i <= pid + (atStart ? 40 : 10); i++) {
    const p = await passage(i);
    if (!p || state.hearings[p.h].id !== h.id) continue;
    const row = turnRow(p, i === pid);
    if (i === pid) row.id = 'anchor';
    if (i !== pid) { row.querySelector('.txt').textContent = p.t; }
    runWrap.appendChild(row);
  }
  const anchor = document.getElementById('anchor');
  if (anchor) anchor.scrollIntoView({ block: 'center' });
}

function closeDrawer() { $('#drawer').classList.remove('open'); }

/* ---------- browse view ---------- */
function renderBrowse() {
  const list = $('#results');
  list.innerHTML = '';
  list.dataset.lastH = '';
  const f = state.filters;
  const hs = state.hearings.filter(h => {
    if (f.committee && !h.committees.includes(f.committee)) return false;
    if (f.from && h.date < f.from) return false;
    if (f.to && h.date > f.to) return false;
    if (f.kind === 'committee' && h.stated) return false;
    if (f.kind === 'stated' && !h.stated) return false;
    return true;
  });
  $('#count').textContent = `${hs.length.toLocaleString()} ${hs.length === 1 ? 'hearing' : 'hearings'}`;
  $('#more').hidden = true;
  $('#export').hidden = true;
  if (!hs.length) { list.appendChild(emptyNote()); return; }
  hs.slice(0, 120).forEach(h => {
    const d = hearingHead(h);
    d.classList.add('browse');
    const n = el('span', 'nmeta', `${h.n.toLocaleString()} passages`);
    d.querySelector('.hh-left').appendChild(n);
    if (h.n) {
      d.classList.add('clickable');
      d.tabIndex = 0;
      d.setAttribute('role', 'button');
      const open = e => {
        if (e.target.closest('a')) return;      // let the document links work
        if (e.type === 'keydown' && e.key !== 'Enter' && e.key !== ' ') return;
        e.preventDefault();
        openDrawer(h.first, h);
      };
      d.addEventListener('click', open);
      d.addEventListener('keydown', open);
    }
    list.appendChild(d);
  });
  if (hs.length > 120) {
    list.appendChild(el('div', 'empty', `Showing the 120 most recent of ${hs.length.toLocaleString()}. Search to reach the rest.`));
  }
}


/* ---------- export the current result set for NotebookLM ----------
   NotebookLM answers questions in plain language but caps words per source, so
   what leaves here is the search result, not the corpus: every matching passage
   with the turns either side of it, and the Legistar link for each hearing. */
const EXPORT_CAP = 500;

async function exportResults() {
  const hits = state.results;
  if (!hits.length) return;
  const btn = $('#export');
  btn.disabled = true;
  btn.textContent = 'Preparing…';

  const take = hits.slice(0, EXPORT_CAP);
  const need = new Set();
  take.forEach(h => {
    for (const pid of [h.pid - 1, h.pid, h.pid + 1]) {
      if (pid >= 0 && pid < state.meta.passages) need.add(Math.floor(pid / state.meta.block));
    }
  });
  await Promise.all([...need].map(getBlock));

  const f = state.filters;
  const scope = [
    f.committee || 'all committees',
    f.from || f.to ? `${f.from || 'the start'} to ${f.to || 'the end'}` : null,
    f.kind === 'committee' ? 'committee hearings only'
      : f.kind === 'stated' ? 'stated meetings only' : null,
    f.role.size ? 'speaking: ' + [...f.role].join(', ') : null,
  ].filter(Boolean).join(' · ');

  const out = [
    `# New York City Council hearings — "${state.q}"\n\n`,
    `Search of the New York City Council hearing record for **${state.q}**.\n`,
    `Filters: ${scope}.\n`,
    `${hits.length.toLocaleString()} matching passages`,
    hits.length > EXPORT_CAP ? `; the ${EXPORT_CAP} best-scoring are included below.\n` : '.\n',
    `\nEach entry is a matching passage with the passage either side of it, so a question and\n`,
    `its answer stay together. Every hearing links to the document it was taken from on\n`,
    `Legistar, which is the record; this file is a derived extract.\n`,
    `\nExported ${new Date().toISOString().slice(0, 10)} from the hearing search.\n`,
  ];

  let lastH = null, n = 0;
  for (const hit of take) {
    const p = await passage(hit.pid);
    if (!p) continue;
    const h = state.hearings[hit.hi];
    if (h.id !== lastH) {
      out.push(`\n\n## ${h.committees.join(' · ')} — ${fmtDate(h.date)}\n`);
      if (h.stated) out.push('_Stated meeting of the full Council._\n');
      h.docs.forEach(d => out.push(
        `- Source: [${d.l}](${d.u})${d.p ? ` (${d.p} pages)` : ''}\n`));
      lastH = h.id;
    }
    n++;
    out.push(`\n### Passage ${n}\n\n`);
    for (const pid of [hit.pid - 1, hit.pid, hit.pid + 1]) {
      const q = await passage(pid);
      if (!q || q.h !== p.h) continue;
      const { name } = speakerLine(q);
      out.push(`**${name}${pid === hit.pid ? ' (match)' : ''}:** ${q.t}\n\n`);
    }
  }

  const blob = new Blob([out.join('')], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `nyc-council-${(state.q || 'search').replace(/[^a-z0-9]+/gi, '-')
    .replace(/^-|-$/g, '').slice(0, 50) || 'search'}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  btn.disabled = false;
  btn.textContent = 'Download for NotebookLM';
}

/* ---------- showing the reader what the search actually did ---------- */
function renderExpansion() {
  const box = $('#expansion');
  box.innerHTML = '';
  const bits = [];
  (state.expansion || []).forEach(e => {
    if (!e.alts.length) return;
    bits.push(`${e.term} also matched ${e.alts.map(a => a.t).join(', ')}`);
  });
  if (state.loosePhrase) bits.push('a quoted phrase was matched as words');
  if (!bits.length) { box.hidden = true; return; }
  box.hidden = false;
  box.textContent = bits.join(' · ');
}

async function renderSuggestions() {
  const box = $('#suggest');
  box.innerHTML = '';
  const miss = (state.missing || []).filter(t => !/^speaker:/.test(t));
  if (!miss.length) { box.hidden = true; return; }
  const word = tokenize(miss[0])[0];
  if (!word) { box.hidden = true; return; }
  const alts = await didYouMean(word);
  if (!alts.length) { box.hidden = true; return; }
  box.hidden = false;
  box.appendChild(document.createTextNode('Did you mean '));
  alts.forEach((a, i) => {
    const link = el('a', null, a);
    link.href = '#';
    link.onclick = e => {
      e.preventDefault();
      $('#q').value = state.q.replace(new RegExp(word, 'i'), a);
      doSearch();
    };
    box.appendChild(link);
    if (i < alts.length - 1) box.appendChild(document.createTextNode(', '));
  });
  box.appendChild(document.createTextNode('?'));
}

/* ---------- wiring ---------- */
let searchToken = 0;
async function doSearch() {
  const q = $('#q').value.trim();
  state.q = q;
  const mine = ++searchToken;
  if (!q) { state.parsed = null; renderBrowse(); syncUrl(); return; }
  $('#count').textContent = 'Searching…';
  const hits = await runSearch(q);
  if (mine !== searchToken) return;

  // highlight everything that actually matched, expansions included
  queryTerms = [];
  (state.expansion || []).forEach(e => {
    queryTerms.push(e.term);
    e.alts.forEach(a => queryTerms.push(a.t));
  });
  (state.parsed.clauses || []).forEach(c => {
    if (c.kind === 'phrase' && !c.neg) c.seq.forEach(t => queryTerms.push(t.term));
    if (c.kind === 'term' && !c.neg) queryTerms.push(c.term);
  });
  queryTerms = [...new Set(queryTerms)];
  renderExpansion();

  const ranges = allowedRanges();
  const roles = await roleSet();
  if (mine !== searchToken) return;

  const kept = [];
  for (const hit of hits) {
    if (ranges && !inRanges(ranges, hit.pid)) continue;
    if (roles && !roles.some(m => m.has(hit.pid))) continue;
    const hi = hearingOf(hit.pid);
    if (hi < 0) continue;
    hit.h = state.hearings[hi].id;
    hit.hi = hi;
    kept.push(hit);
  }
  if (mine !== searchToken) return;

  // keep hearings together, newest first, best passage first within a hearing
  const order = new Map();
  kept.forEach(h => { const b = order.get(h.h); if (!b || h.score > b) order.set(h.h, h.score); });
  kept.sort((a, b) => (order.get(b.h) - order.get(a.h)) ||
                      (a.hi - b.hi) ||
                      (b.score - a.score));

  state.results = kept;
  $('#results').dataset.lastH = '';
  await render(true);
  syncUrl();
}

function syncUrl() {
  const p = new URLSearchParams();
  if (state.q) p.set('q', state.q);
  const f = state.filters;
  if (f.committee) p.set('c', f.committee);
  if (f.from) p.set('from', f.from);
  if (f.to) p.set('to', f.to);
  if (f.kind) p.set('kind', f.kind);
  if (f.role.size) p.set('role', [...f.role].join(','));
  if (state.exact) p.set('exact', '1');
  const s = p.toString();
  history.replaceState(null, '', s ? '?' + s : location.pathname);
}

function readUrl() {
  const p = new URLSearchParams(location.search);
  state.q = p.get('q') || '';
  state.filters.committee = p.get('c') || '';
  state.filters.from = p.get('from') || '';
  state.filters.to = p.get('to') || '';
  state.filters.kind = p.get('kind') || '';
  (p.get('role') || '').split(',').filter(Boolean).forEach(r => state.filters.role.add(r));
  state.exact = p.get('exact') === '1';
}

async function init() {
  const [meta, hearings] = await Promise.all([
    fetch(DATA + 'meta.json').then(r => r.json()),
    fetch(DATA + 'hearings.json').then(r => r.json()),
  ]);
  state.meta = meta; state.hearings = hearings;

  $('#n-hearings').textContent = meta.hearings.toLocaleString();
  $('#n-transcripts').textContent = meta.transcripts.toLocaleString();
  $('#n-turns').textContent = meta.turns.toLocaleString();
  $('#n-written').textContent = meta.written.toLocaleString();
  $('#capline').innerHTML =
    `Covering <b>${fmtDate(meta.first_date)}</b> to <b>${fmtDate(meta.last_date)}</b> · built ${meta.built}`;

  const sel = $('#committee');
  meta.committees.forEach(c => {
    const o = el('option', null, c); o.value = c; sel.appendChild(o);
  });

  readUrl();
  $('#q').value = state.q;
  sel.value = state.filters.committee;
  $('#from').value = state.filters.from;
  $('#to').value = state.filters.to;
  document.querySelectorAll('[data-role]').forEach(b => {
    b.setAttribute('aria-pressed', state.filters.role.has(b.dataset.role));
  });
  document.querySelectorAll('[data-kind]').forEach(b => {
    b.setAttribute('aria-pressed', state.filters.kind === b.dataset.kind);
  });

  let t;
  $('#q').addEventListener('input', () => { clearTimeout(t); t = setTimeout(doSearch, 220); });
  $('#q').addEventListener('keydown', e => { if (e.key === 'Enter') { clearTimeout(t); doSearch(); } });
  sel.addEventListener('change', () => { state.filters.committee = sel.value; refilter(); });
  $('#from').addEventListener('change', () => { state.filters.from = $('#from').value; refilter(); });
  $('#to').addEventListener('change', () => { state.filters.to = $('#to').value; refilter(); });
  document.querySelectorAll('[data-role]').forEach(b => b.onclick = () => {
    const r = b.dataset.role;
    state.filters.role.has(r) ? state.filters.role.delete(r) : state.filters.role.add(r);
    b.setAttribute('aria-pressed', state.filters.role.has(r));
    refilter();
  });
  document.querySelectorAll('[data-kind]').forEach(b => b.onclick = () => {
    state.filters.kind = state.filters.kind === b.dataset.kind ? '' : b.dataset.kind;
    document.querySelectorAll('[data-kind]').forEach(x =>
      x.setAttribute('aria-pressed', state.filters.kind === x.dataset.kind));
    refilter();
  });
  $('#clear').onclick = () => {
    state.filters = { role: new Set(), committee: '', from: '', to: '', kind: '' };
    sel.value = ''; $('#from').value = ''; $('#to').value = '';
    document.querySelectorAll('[aria-pressed]').forEach(x => x.setAttribute('aria-pressed', 'false'));
    refilter();
  };
  $('#more').onclick = () => render(false);
  $('#export').onclick = exportResults;
  const ex = $('#exact');
  ex.setAttribute('aria-pressed', String(state.exact));
  ex.onclick = () => {
    state.exact = !state.exact;
    ex.setAttribute('aria-pressed', String(state.exact));
    doSearch();
  };
  $('#tips-toggle').onclick = () => {
    const t = $('#tips');
    t.hidden = !t.hidden;
    $('#tips-toggle').setAttribute('aria-expanded', String(!t.hidden));
  };
  getDict();   // warm the vocabulary in the background
  $('#dclose').onclick = closeDrawer;
  $('#drawer .scrim').onclick = closeDrawer;
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeDrawer();
    if (e.key === '/' && document.activeElement !== $('#q')) {
      e.preventDefault(); $('#q').focus(); $('#q').select();
    }
  });
  document.querySelectorAll('[data-example]').forEach(a => a.onclick = e => {
    e.preventDefault(); $('#q').value = a.dataset.example; doSearch();
  });

  if (state.q) doSearch(); else renderBrowse();
}

function refilter() { doSearch(); }

init();
