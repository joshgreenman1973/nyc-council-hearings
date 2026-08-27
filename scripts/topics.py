#!/usr/bin/env python3
"""Group hearings under topics a reader would actually browse by.

The Council's own filing has committees, not topics, and the two do not line
up: housing turns up in Housing and Buildings, in Finance, in Land Use and in
General Welfare. So a topic is assigned from two things the record already
states -- the committee that sat, and the subject line of what was on the
agenda ("Oversight - Implementation of the Pied-a-Terre Tax").

This is our editorial grouping, not the Council's, and the site says so. The
committee mapping is deterministic; the keyword rules only ever add a topic,
never remove one, so a hearing can sit under more than one.
"""
import re

# Committee -> topic. The name is matched as a lowercase substring, longest
# first, so "environmental protection" wins over "protection".
BY_COMMITTEE = [
    ('aging',                                   'Aging'),
    ('children and youth',                      'Children and youth'),
    ('civil and human rights',                  'Civil rights'),
    ('civil service and labor',                 'Jobs and labor'),
    ('consumer and worker protection',          'Jobs and labor'),
    ('contracts',                               'Contracts and procurement'),
    ('criminal justice',                        'Criminal justice'),
    ('cultural affairs',                        'Arts and culture'),
    ('economic development',                    'Economy and business'),
    ('education',                               'Education'),
    ('environmental protection',                'Environment'),
    ('environment protection',                  'Environment'),
    ('finance',                                 'Budget and taxes'),
    ('fire and emergency management',           'Public safety'),
    ('general welfare',                         'Poverty and social services'),
    ('governmental operations',                 'How government works'),
    ('government operations',                   'How government works'),
    ('health',                                  'Health'),
    ('higher education',                        'Education'),
    ('hospitals',                               'Health'),
    ('housing and buildings',                   'Housing'),
    ('immigration',                             'Immigration'),
    ('land use',                                'Land use and zoning'),
    ('landmarks',                               'Land use and zoning'),
    ('mental health',                           'Health'),
    ('oversight and investigations',            'Oversight'),
    ('parks and recreation',                    'Parks and public space'),
    ('planning, dispositions and concessions',  'Land use and zoning'),
    ('public housing',                          'Housing'),
    ('public safety',                           'Public safety'),
    ('resiliency and waterfronts',              'Environment'),
    ('rules, privileges and elections',         'How government works'),
    ('sanitation and solid waste',              'Environment'),
    ('small business',                          'Economy and business'),
    ('standards and ethics',                    'How government works'),
    ('state and federal legislation',           'How government works'),
    ('technology',                              'Technology and data'),
    ('transportation and infrastructure',       'Transportation'),
    ('veterans',                                'Veterans'),
    ('women and gender equity',                 'Civil rights'),
    ('zoning and franchises',                   'Land use and zoning'),
]

# Subject keyword -> topic. Applied to the agenda's own subject lines, to catch
# what the committee alone would miss: a housing bill heard in Finance, a
# policing question raised at a stated meeting.
BY_SUBJECT = [
    (r'\bhous(ing|e)\b|\brent\b|\brents\b|\bnycha\b|\blandlord|\btenant|\bevict|'
     r'\bhomeless|\bshelter\b|\bvoucher|\bcityfheps\b|\bmitchell-lama\b|\brezon',  'Housing'),
    (r'\bnypd\b|\bpolic(e|ing)\b|\bofficer|\bprecinct|\bcrime\b|\bshooting|\bgun\b|'
     r'\bsubway safety\b|\bsurveillance\b|\bpatrol\b',                             'Public safety'),
    (r'\bjail|\brikers\b|\bincarcerat|\bcorrection|\bprobation\b|\bparole\b|'
     r'\bre-?entry\b|\bdistrict attorney\b|\bbail\b',                              'Criminal justice'),
    (r'\bschool|\bstudent|\bteacher|\bdoe\b|\bclass size\b|\bcuny\b|\bpre-?k\b|'
     r'\b3-?k\b|\bcharter school',                                                 'Education'),
    (r'\bhealth\b|\bhospital|\bmedicaid\b|\bmental health\b|\boverdose\b|'
     r'\bopioid\b|\bmaternal\b|\bnursing home',                                    'Health'),
    (r'\bbus(es)?\b|\bsubway\b|\bmta\b|\bbike\b|\bstreet safety\b|\bvision zero\b|'
     r'\bparking\b|\bcongestion pricing\b|\bferry\b|\btaxi\b|\bfor-hire\b|'
     r'\bdelivery worker|\bsidewalk',                                              'Transportation'),
    (r'\bbudget\b|\btax\b|\btaxes\b|\brevenue\b|\bfiscal\b|\bappropriat|'
     r'\bcapital plan\b|\bcomptroller\b',                                          'Budget and taxes'),
    (r'\bimmigra|\basylum\b|\bmigrant|\bice\b|\bsanctuary\b|\bundocumented\b',      'Immigration'),
    (r'\bclimate\b|\bemissions\b|\bflood|\bresilien|\bwaste\b|\brecycl|\bcompost|'
     r'\bair quality\b|\brat\b|\brats\b|\bsanitation\b|\bwater\b|\bsewer',          'Environment'),
    (r'\bworker|\bwage|\bunion\b|\bemployment\b|\bjobs?\b|\bhiring\b|'
     r'\bcivil service\b|\blabor\b',                                               'Jobs and labor'),
    (r'\bsmall business|\bstorefront|\bcommercial rent\b|\bmwbe\b|\bminority.and.women',
                                                                                   'Economy and business'),
    (r'\bchild care\b|\bchildcare\b|\bfoster\b|\bacs\b|\byouth\b|\bafter-?school\b|'
     r'\bsummer youth\b',                                                          'Children and youth'),
    (r'\bsenior|\bolder adult|\baging\b',                                          'Aging'),
    (r'\bdisabilit|\baccessib',                                                    'Disability'),
    (r'\bveteran',                                                                 'Veterans'),
    (r'\bpark\b|\bparks\b|\bplayground|\bopen space\b|\bpublic space\b',            'Parks and public space'),
    (r'\bartificial intelligence\b|\balgorithm|\bbroadband\b|\bcyber|\bdata\b|'
     r'\bopen data\b|\btechnology\b',                                              'Technology and data'),
    (r'\bprocurement\b|\bcontract',                                                'Contracts and procurement'),
    (r'\bzoning\b|\bland use\b|\buniform land use\b|\bulurp\b|\blandmark',          'Land use and zoning'),
    (r'\bcharter revision\b|\belection|\bballot\b|\bvoting\b|\bethics\b|'
     r'\bcampaign finance\b|\bredistrict',                                         'How government works'),
    (r'\bfood\b|\bhunger\b|\bpantry\b|\bsnap\b|\bcash assistance\b|\bpoverty\b',    'Poverty and social services'),
    (r'\bculture\b|\bcultural\b|\blibrar|\bmuseum|\bthe arts\b',                    'Arts and culture'),
]

SUBJECT_RULES = [(re.compile(p, re.I), t) for p, t in BY_SUBJECT]

ALL_TOPICS = sorted({t for _, t in BY_COMMITTEE} | {t for _, t in BY_SUBJECT})


def committee_topic(name):
    low = (name or '').lower()
    hits = [(len(k), t) for k, t in BY_COMMITTEE if k in low]
    if not hits:
        return None
    hits.sort(reverse=True)
    return hits[0][1]


def topics_for(committees, subjects, stated=False):
    """Topics for one hearing, most confident first."""
    out = []
    for c in committees or []:
        t = committee_topic(c)
        if t and t not in out:
            out.append(t)
    text = ' \n '.join(subjects or [])[:20000]
    for rx, t in SUBJECT_RULES:
        if t in out:
            continue
        if rx.search(text):
            out.append(t)
    if not out:
        out.append('Stated business' if stated else 'Other')
    return out[:6]


def slug(t):
    return re.sub(r'[^a-z0-9]+', '-', t.lower()).strip('-')
