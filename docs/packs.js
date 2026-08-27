/* Building a NotebookLM pack in the browser.

   The whole corpus as Markdown is a quarter of a gigabyte, which has no
   business sitting in the repository when the passage blocks the search
   already downloads contain every word of it. A pack is assembled here, on
   demand, from those same blocks. */

const DATA = 'data/';
const MONTHS = ['Jan.', 'Feb.', 'March', 'April', 'May', 'June', 'July', 'Aug.',
                'Sept.', 'Oct.', 'Nov.', 'Dec.'];
const WORDS_PER_PART = 380000;      // under NotebookLM's per-source ceiling
const WORDS_PER_PASSAGE = 118;      // measured across this corpus, for estimates

let META = null, HEARINGS = null;
const blocks = new Map();

const $ = s => document.querySelector(s);
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; };
const fmt = d => { const [y, m, dd] = d.split('-').map(Number); return `${MONTHS[m - 1]} ${dd}, ${y}`; };

async function getBlock(n) {
  if (!blocks.has(n)) blocks.set(n, fetch(`${DATA}psg/${n}.json`).then(r => r.ok ? r.json() : null));
  return blocks.get(n);
}

async function passagesOf(h, onProgress) {
  const out = [];
  const first = h.first, last = h.first + h.n;
  const b0 = Math.floor(first / META.block), b1 = Math.floor((last - 1) / META.block);
  for (let b = b0; b <= b1; b++) {
    const blk = await getBlock(b);
    if (!blk) continue;
    const base = b * META.block;
    for (let i = 0; i < blk.p.length; i++) {
      const pid = base + i;
      if (pid < first || pid >= last) continue;
      const row = blk.p[i];
      out.push({ s: blk.sp[row[0]], r: blk.ro[row[1]], t: row[2] });
    }
    if (onProgress) onProgress();
  }
  return out;
}

function groupKey(h) {
  return h.stated ? 'Stated meetings of the Council'
                  : (h.committees[0] || 'New York City Council');
}

function groups() {
  const g = new Map();
  HEARINGS.forEach(h => {
    const k = groupKey(h);
    if (!g.has(k)) g.set(k, []);
    g.get(k).push(h);
  });
  return [...g.entries()]
    .map(([name, hs]) => ({
      name, hs,
      passages: hs.reduce((n, h) => n + h.n, 0),
      first: hs[hs.length - 1].date, last: hs[0].date,
    }))
    .sort((a, b) => b.passages - a.passages);
}

function download(name, text) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }));
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 8000);
}

function slug(s) {
  return s.toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '')
    .replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 70);
}

async function buildPack(group, btn) {
  const original = btn.textContent;
  btn.disabled = true;
  let done = 0;
  const total = group.hs.length;

  const parts = [];
  let buf = [`# ${group.name}\n\n`,
             `New York City Council hearing record — ${group.name}.\n`,
             `${total} hearings, ${fmt(group.first)} to ${fmt(group.last)}.\n`,
             `Transcripts and written testimony as published by the Council on Legistar.\n`,
             `Assembled ${new Date().toISOString().slice(0, 10)}.\n`];
  let words = 0;

  for (const h of group.hs) {
    done++;
    btn.textContent = `Building… ${done}/${total}`;
    const ps = await passagesOf(h);
    const chunk = [`\n\n## ${h.committees.join(' · ')} — ${fmt(h.date)}\n`];
    if (h.stated) chunk.push('_Stated meeting of the full Council._\n');
    h.docs.forEach(d => chunk.push(
      `- Source: [${d.l}](${d.u})${d.p ? ` (${d.p} pages)` : ''}\n`));
    chunk.push('\n');
    let lastKind = null;
    ps.forEach(p => {
      const kind = p.r === 'written' ? 'written' : 'spoken';
      if (kind !== lastKind) {
        chunk.push(kind === 'written' ? '\n### Written testimony\n\n' : '\n### Hearing transcript\n\n');
        lastKind = kind;
      }
      chunk.push(`**${p.s}:** ${p.t}\n\n`);
    });
    const text = chunk.join('');
    const w = (text.match(/\s+/g) || []).length;
    if (words && words + w > WORDS_PER_PART) { parts.push(buf.join('')); buf = [buf[0]]; words = 0; }
    buf.push(text); words += w;
    if (done % 5 === 0) await new Promise(r => setTimeout(r, 0));   // keep the page responsive
  }
  parts.push(buf.join(''));

  const base = slug(group.name);
  parts.forEach((text, i) => {
    const name = parts.length === 1 ? `${base}.md` : `${base}-part-${i + 1}.md`;
    setTimeout(() => download(name, text), i * 700);
  });

  btn.disabled = false;
  btn.textContent = parts.length === 1 ? original : `${original} (${parts.length} files)`;
}

async function init() {
  [META, HEARINGS] = await Promise.all([
    fetch(DATA + 'meta.json').then(r => r.json()),
    fetch(DATA + 'hearings.json').then(r => r.json()),
  ]);
  const gs = groups();
  $('#packcount').textContent = `${gs.length} committees`;
  const tb = $('#rows');
  gs.forEach(g => {
    const tr = document.createElement('tr');
    const words = g.passages * WORDS_PER_PASSAGE;
    const parts = Math.max(1, Math.ceil(words / WORDS_PER_PART));
    const td0 = document.createElement('td');
    const btn = el('button', 'chip', 'Build pack');
    btn.onclick = () => buildPack(g, btn);
    td0.appendChild(btn);
    tr.appendChild(td0);
    tr.appendChild(el('td', null, g.name));
    const n1 = el('td', 'n', g.hs.length.toLocaleString()); tr.appendChild(n1);
    const n2 = el('td', 'n', '~' + Math.round(words / 1000).toLocaleString() + 'k'); tr.appendChild(n2);
    const n3 = el('td', 'n', String(parts)); tr.appendChild(n3);
    tr.appendChild(el('td', null, `${fmt(g.first)} – ${fmt(g.last)}`));
    tb.appendChild(tr);
  });
}

init();
