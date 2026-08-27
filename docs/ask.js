/* Turning a plain-language question into a query this index can answer.

   Nothing here is a language model. It is a set of rules that read the parts of
   a question that map onto things the corpus actually knows: who was speaking,
   when, on what subject. Whatever is left becomes the search terms, widened
   with synonyms so that a question asking about "rising" assaults still finds
   the official who said they were "up by 9%".

   The reading is always shown to the user and can always be overridden. */

/* Mayoral terms, so "what did the Mamdani administration say" becomes a date
   range. "Adams" has to be qualified: Adrienne Adams was Council Speaker for
   most of this corpus, and she is not the Adams administration. */
const ERAS = [
  { rx: /\b(mamdani|current|this)\s+administration\b/i,
    label: 'Mamdani administration', role: 'administration', after: '2026-01-01' },
  { rx: /\b(adams|previous|prior|last)\s+administration\b/i,
    label: 'Adams administration', role: 'administration', before: '2025-12-31' },
];

const ROLE_HINTS = [
  { rx: /\b(?:the\s+)?administration\b|\bcity\s+hall\b|\bmayor'?s?\s+office\b|\bcommissioners?\b|\bagenc(?:y|ies)\b|\bcity\s+officials?\b|\bdeputy\s+mayor\b/i,
    role: 'administration', label: 'administration witnesses' },
  { rx: /\bcouncil\s*members?\b|\bthe\s+council\b|\blegislators?\b|\bspeaker\s+of\s+the\s+council\b/i,
    role: 'council', label: 'council members' },
  { rx: /\bmembers\s+of\s+the\s+public\b|\bpublic\s+witnesses?\b|\bordinary\s+new\s+yorkers\b|\badvocates?\b|\bresidents?\s+(?:said|told|testified|say)\b/i,
    role: 'public', label: 'public witnesses' },
  { rx: /\bwritten\s+testimony\b|\bsubmitted\s+testimony\b/i,
    role: 'written', label: 'written testimony' },
];

/* Words a question uses that the record says differently. These become
   alternatives, not replacements, so the original word still counts. */
const SYNONYMS = {
  rising: ['rise', 'rises', 'increase', 'increases', 'increasing', 'increased', 'up',
           'higher', 'growth', 'grew', 'surge', 'spike', 'climbing', 'uptick'],
  falling: ['fall', 'falls', 'decline', 'declining', 'declined', 'decrease', 'decreased',
            'down', 'lower', 'drop', 'dropped', 'reduction'],
  reason: ['reasons', 'cause', 'causes', 'caused', 'causing', 'driver', 'drivers',
           'driving', 'because', 'attributable', 'explanation', 'why', 'behind',
           'factor', 'factors'],
  assault: ['assaults'],
  cost: ['costs', 'spending', 'expenditure', 'price'],
  homeless: ['homelessness', 'shelter', 'unsheltered'],
  police: ['nypd', 'officers', 'policing'],
  schools: ['school', 'doe', 'students', 'classrooms'],
  housing: ['apartments', 'units', 'nycha', 'rent'],
  crime: ['crimes', 'criminal', 'offenses'],
  hiring: ['hire', 'hired', 'recruitment', 'headcount', 'vacancies'],
  delay: ['delays', 'delayed', 'backlog', 'wait', 'waiting'],
};

/* Causal questions want the passage that explains, not merely mentions. */
const CAUSAL_RX = /\bwhy\b|\breasons?\b|\bcauses?\b|\bbehind\b|\bdriving\b|\bdrivers?\b|\bexplain|\baccount\s+for\b|\bblame|\battribut/i;
const CAUSAL_TERMS = ['because', 'due', 'reason', 'cause', 'driven', 'driving',
                      'attributable', 'result', 'factor', 'explains'];

/* Scaffolding: the words that make a sentence a question rather than a subject. */
const SCAFFOLD = new Set(`what which who whom whose when where why how did does do is are was
were has have had will would could should can may might say says said saying tell told
about behind according regarding concerning think thinks any anyone anybody there here
give given tells state stated mention mentioned number numbers amount level levels rate
rates figure figures please me us you your their his her its it's the a an and or but of
for from with that this these those been being over under into than then also more most
some such only very just now new york city nyc`.split(/\s+/));

const RELATIVE_DATES = [
  { rx: /\bthis\s+year\b/i, after: () => `${new Date().getFullYear()}-01-01`, label: 'this year' },
  { rx: /\blast\s+year\b/i,
    after: () => `${new Date().getFullYear() - 1}-01-01`,
    before: () => `${new Date().getFullYear() - 1}-12-31`, label: 'last year' },
  { rx: /\bsince\s+(\d{4})\b/i, afterFrom: m => `${m[1]}-01-01`, label: null },
  { rx: /\bin\s+(20\d\d)\b/i, afterFrom: m => `${m[1]}-01-01`,
    beforeFrom: m => `${m[1]}-12-31`, label: null },
];

function looksLikeQuestion(q) {
  const s = q.trim();
  if (!s || /[:*~"]/.test(s)) return false;          // already using the operators
  if (s.endsWith('?')) return true;
  const words = s.split(/\s+/);
  if (words.length < 5) return false;
  return /^(what|which|who|whom|whose|when|where|why|how|did|does|do|is|are|was|were|has|have|had|can|could|should|would|tell|show|find)\b/i.test(s);
}

/* Returns {filters, terms, causal, reading} or null if this is not a question. */
function translateQuestion(q, opts) {
  if (!looksLikeQuestion(q)) return null;
  const meta = (opts && opts.meta) || {};
  const speakers = (opts && opts.speakers) || [];
  const reading = [];
  const filters = { role: [], topic: [], after: '', before: '', speaker: [] };
  let rest = ' ' + q + ' ';

  const eat = (rx) => { rest = rest.replace(rx, ' '); };

  for (const e of ERAS) {
    if (e.rx.test(rest)) {
      filters.role.push(e.role);
      if (e.after) filters.after = e.after;
      if (e.before) filters.before = e.before;
      reading.push(e.label);
      eat(e.rx);
      break;
    }
  }
  if (!filters.role.length) {
    for (const h of ROLE_HINTS) {
      if (h.rx.test(rest)) {
        filters.role.push(h.role);
        reading.push(h.label);
        eat(h.rx);
        break;
      }
    }
  }

  for (const r of RELATIVE_DATES) {
    const m = r.rx.exec(rest);
    if (!m) continue;
    if (r.after) filters.after = filters.after || r.after();
    if (r.before) filters.before = filters.before || r.before();
    if (r.afterFrom) filters.after = filters.after || r.afterFrom(m);
    if (r.beforeFrom) filters.before = filters.before || r.beforeFrom(m);
    if (r.label) reading.push(r.label);
    eat(r.rx);
    break;
  }

  // A named person, matched against the speakers actually in the corpus.
  const nameSet = new Set(speakers);
  const words = (rest.match(/[A-Za-z'’-]{3,}/g) || []);
  for (const w of words) {
    const low = w.toLowerCase();
    if (SCAFFOLD.has(low) || low.length < 4) continue;
    if (nameSet.has(low) && /^[A-Z]/.test(w)) {
      filters.speaker.push(low);
      reading.push(`speaker: ${w}`);
      rest = rest.replace(new RegExp('\\b' + w + '\\b', 'g'), ' ');
      break;
    }
  }

  // A topic, if the question names one.
  const topicNames = (meta.topics || []).map(t => t[0]);
  for (const name of topicNames) {
    const head = name.split(/\s+and\s+/)[0].toLowerCase();
    if (head.length > 4 && new RegExp('\\b' + head + '\\w*\\b', 'i').test(rest)) {
      filters.topic.push(name.toLowerCase().replace(/[^a-z0-9]+/g, '-'));
      reading.push(`topic: ${name}`);
      break;
    }
  }

  const causal = CAUSAL_RX.test(q);

  // Whatever is left is what the question is actually about.
  const terms = [];
  for (const w of (rest.match(/[a-z0-9'’-]{2,}/gi) || [])) {
    const low = w.toLowerCase().replace(/^[-']+|[-']+$/g, '');
    if (!low || low.length < 3 || SCAFFOLD.has(low)) continue;
    if (terms.some(t => t.term === low)) continue;
    // A question says "assaults" where the synonym table is keyed on "assault".
    let alts = SYNONYMS[low];
    if (!alts && low.endsWith('s')) {
      const sing = low.slice(0, -1);
      if (SYNONYMS[sing]) alts = [sing, ...SYNONYMS[sing]];
    }
    terms.push({ term: low, alts: (alts || []).filter(a => a !== low) });
  }

  return { filters, terms, causal, causalTerms: causal ? CAUSAL_TERMS : [], reading, original: q };
}

window.translateQuestion = translateQuestion;
window.looksLikeQuestion = looksLikeQuestion;
