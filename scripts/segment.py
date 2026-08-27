#!/usr/bin/env python3
"""Turn cleaned transcript text into attributed speaker turns.

A turn is the atomic searchable unit: one person talking, once. A search hit is
shown as its turn plus the neighbours on either side, which is what makes a
question and its answer read as a single exchange.
"""
import re, unicodedata

# "COMMISSIONER TISCH:", "COUNCIL MEMBER J. SANCHEZ:", "MALE SPEAKER:"
LABEL = re.compile(r'(?:(?<=^)|(?<=[\s\.\?\!"\)\]]))'
                   r'([A-Z][A-Z][A-Z \.\'\-]{1,58}?):(?=\s)')

COUNCIL_TITLES = ('COUNCIL MEMBER', 'CHAIRPERSON', 'CHAIRMAN', 'CHAIRWOMAN', 'CHAIR',
                  'SPEAKER ADAMS', 'SPEAKER MENIN', 'SPEAKER', 'MAJORITY LEADER',
                  'MINORITY LEADER', 'PUBLIC ADVOCATE', 'BOROUGH PRESIDENT',
                  'COMPTROLLER', 'CO-CHAIR')
AGENCY_TITLES = ('COMMISSIONER', 'DEPUTY COMMISSIONER', 'FIRST DEPUTY COMMISSIONER',
                 'ASSISTANT COMMISSIONER', 'ASSOCIATE COMMISSIONER', 'ACTING COMMISSIONER',
                 'CHIEF', 'DEPUTY CHIEF', 'ASSISTANT CHIEF', 'INSPECTOR', 'DIRECTOR',
                 'EXECUTIVE DIRECTOR', 'DEPUTY DIRECTOR', 'ADMINISTRATOR', 'CHANCELLOR',
                 'DEPUTY MAYOR', 'PRESIDENT', 'GENERAL COUNSEL', 'SUPERINTENDENT',
                 'CAPTAIN', 'SERGEANT', 'LIEUTENANT', 'COLONEL')
STAFF_TITLES = ('COMMITTEE COUNSEL', 'COUNSEL', 'SERGEANT AT ARMS', 'SERGEANT-AT-ARMS',
                'CLERK', 'MODERATOR', 'LEGISLATIVE', 'POLICY ANALYST', 'FINANCE ANALYST',
                'COMMITTEE CLERK')
GENERIC = ('MALE SPEAKER', 'FEMALE SPEAKER', 'UNIDENTIFIED', 'UNKNOWN', 'AUDIENCE',
           'MULTIPLE SPEAKERS', 'CROWD', 'VOICE')

# things that look like a label but are not one
NOT_A_LABEL = re.compile(
    r'^(?:A P P E A R A N C E S|APPEARANCES|HELD AT|B E F O R E|BEFORE|NOTE|'
    r'PRESENT|ALSO PRESENT|START|RECESS|CONTINUED|EXHIBIT|ATTACHMENT|'
    r'RESPECTFULLY SUBMITTED|WHEREUPON|SUBJECT|RE|CC|FROM|TO|DATE|PAGE|'
    r'QUESTION|ANSWER|WITNESS|COUNCIL MEMBERS)$')


def classify(label):
    u = label.upper().strip()
    for g in GENERIC:
        if u.startswith(g):
            return 'unidentified'
    for t in STAFF_TITLES:
        if u.startswith(t):
            return 'staff'
    for t in sorted(COUNCIL_TITLES, key=len, reverse=True):
        if u.startswith(t):
            return 'council'
    for t in sorted(AGENCY_TITLES, key=len, reverse=True):
        if u.startswith(t):
            return 'administration'
    if re.match(r'^(MR|MS|MRS|MISS|DR|REV|RABBI|IMAM|PROFESSOR|PROF)\b', u):
        return 'public'
    return 'other'


def surname(label):
    """The name part of a speaker label, with the honorific/title removed."""
    u = re.sub(r'\s+', ' ', label.upper().strip())
    for t in sorted(COUNCIL_TITLES + AGENCY_TITLES + STAFF_TITLES, key=len, reverse=True):
        if u.startswith(t + ' '):
            return u[len(t) + 1:].strip()
    m = re.match(r'^(?:MR|MS|MRS|MISS|DR|REV|RABBI|IMAM|PROFESSOR|PROF)\.?\s+(.*)$', u)
    if m:
        return m.group(1).strip()
    return u


def split_turns(text):
    """Split cleaned transcript text into [{speaker, role, text, start}]."""
    marks = []
    for m in LABEL.finditer(text):
        lab = re.sub(r'\s+', ' ', m.group(1)).strip(" .'-")
        if not lab or NOT_A_LABEL.match(lab):
            continue
        if len(lab) > 60 or lab.count(' ') > 5:
            continue
        marks.append((m.start(1), m.end(0), lab))
    turns = []
    for i, (s, e, lab) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[e:end].strip()
        if not body:
            continue
        turns.append({
            'speaker': lab,
            'name': surname(lab),
            'role': classify(lab),
            'text': body,
            'start': s,
        })
    return turns


COVER_DATE = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|October|'
    r'November|December)\s+(\d{1,2}),?\s+(\d{4})', re.I)
MONTHS = {m: i for i, m in enumerate(
    ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
     'september', 'october', 'november', 'december'], 1)}


def parse_cover(first_pages):
    """Committees and date declared on the transcript's own cover page."""
    head = ' '.join(first_pages)[:4000]
    out = {}
    # Joint hearings are written either "COMMITTEE ON A Jointly with COMMITTEE
    # ON B" or entirely in caps as "... JOINTLY WITH THE COMMITTEE ON B". In the
    # second form the capture runs straight through the joiner and the two
    # committees come out as one long name, so break the joiners first.
    split = re.sub(r'\s+JOINTLY\s+WITH\s+(?:THE\s+)?', ' | ', head, flags=re.I)
    split = re.sub(r',?\s+AND\s+(?:THE\s+)?(?=(?:COMMITTEE|SUBCOMMITTEE)\s+ON)',
                   ' | ', split, flags=re.I)
    split = re.sub(r',\s+(?=(?:COMMITTEE|SUBCOMMITTEE)\s+ON)', ' | ', split, flags=re.I)
    comms = re.findall(r'((?:COMMITTEE|SUBCOMMITTEE)\s+ON\s+[A-Z][A-Z &,\'\-\.]{3,80})', split)
    seen, clean = set(), []
    for c in comms:
        c = re.sub(r'\s+', ' ', c).strip(' ,.-')
        # Committee names are set in caps and the joiners ("Jointly with",
        # "And") are not, so the capture stops on its own -- but it swallows the
        # joiner's capitalised first letter, and the cover's rule line.
        c = re.split(r'\s*(?:-{3,}|\|)', c)[0]
        c = re.sub(r'\s+[A-Z]$', '', c).strip(' ,.-')
        c = re.sub(r'\s+(?:AND|WITH|JOINTLY)$', '', c).strip(' ,.-')
        if len(c) < 14:
            continue
        c = re.sub(r'\s+(?:VOTE|HEARING|MEETING)$', '', c, flags=re.I)
        c = re.sub(r'\s*&\s*', ' and ', c)      # "Fire & Emergency" == "Fire and Emergency"
        c = re.sub(r'\s+', ' ', c).strip(' ,.-')
        if len(c) < 14:
            continue
        if c not in seen:
            seen.add(c); clean.append(c.title().replace(' On ', ' on ')
                                       .replace(' And ', ' and ').replace(' Of ', ' of ')
                                       .replace(' The ', ' the '))
    if clean:
        out['committees'] = clean[:4]
    if re.search(r'STATED\s+MEETING', head, re.I):
        out['stated_meeting'] = True
    m = COVER_DATE.search(head)
    if m:
        mo = MONTHS.get(m.group(1).lower())
        if mo:
            out['date'] = f'{int(m.group(3))}-{mo:02d}-{int(m.group(2)):02d}'
    st = re.search(r'Start:\s*([\d:]+\s*[ap]\.?m\.?)', head, re.I)
    if st:
        out['start_time'] = st.group(1)
    return out
