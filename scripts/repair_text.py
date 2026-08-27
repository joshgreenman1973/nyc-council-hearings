#!/usr/bin/env python3
"""Apply extraction repairs to the already-extracted text. Idempotent."""
import glob, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from repair import repair, load_words, load_freq, fix_runs

TXT = os.path.join(HERE, '..', 'data', 'text')
DAMAGED = 0.10          # space ratio below which a document lost its word breaks


def main():
    words, freq = load_words(), load_freq()
    files = sorted(glob.glob(os.path.join(TXT, '*.txt')))
    changed = repaired_runs = 0
    for i, f in enumerate(files):
        t = open(f).read()
        if len(t) < 200:
            continue
        ratio = t.count(' ') / len(t)
        out = repair(t, words=words)
        if ratio < DAMAGED:
            before = out.count(' ')
            out = fix_runs(out, freq, threshold=12)
            if out.count(' ') > before:
                repaired_runs += 1
        if out != t:
            open(f, 'w').write(out)
            changed += 1
        if i % 400 == 0 and i:
            print(f'  {i}/{len(files)}', flush=True)
    print(f'repaired {changed} of {len(files)} documents; '
          f'{repaired_runs} had their word spacing rebuilt', flush=True)


if __name__ == '__main__':
    main()
