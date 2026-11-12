#!/usr/bin/env python3
"""Discover Internet Archive videos with permissive *declared* reuse licenses.
This is metadata screening, not proof of rights or successful playback.
Runs on GitHub Actions, not the memory-constrained IPTV server.
"""
import concurrent.futures
import collections
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

OUT = Path('external_movies.json')
REPORT = Path('external_catalog_report.json')
MAX_ITEMS = 150
PER_QUERY = 100
UA = 'OG-IPTV-metadata-discovery/2.0'
SEARCHES = [
    'mediatype:movies AND (language:persian OR language:farsi OR language:fas OR language:per OR language:dari)',
    'mediatype:movies AND (title:persian OR title:iran OR title:afghan OR subject:persian OR subject:iran)',
    'mediatype:movies AND licenseurl:creativecommons.org/licenses/*',
    'mediatype:movies AND licenseurl:creativecommons.org/publicdomain/*',
]
# BY and BY-SA allow redistribution under conditions. ND/NC excluded: service use and
# adaptations are context-dependent; no assumption that commercial redistribution is permitted.
LICENSE = re.compile(r'^https?://creativecommons\.org/(?:publicdomain/(?:zero|mark)/1\.0|licenses/(?:by|by-sa)/(?:2\.0|2\.5|3\.0|4\.0))(?:/|$)', re.I)
VIDEO_EXT = ('.mp4', '.webm', '.ogv', '.m4v')

def get(url, limit=6_000_000):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('response too large')
    return json.loads(raw)

def scalar(v):
    if isinstance(v, list):
        return ' '.join(str(x) for x in v[:5])
    return str(v or '')

def search(q):
    params = urllib.parse.urlencode({'q': q, 'fl[]': ['identifier', 'title', 'language', 'subject'], 'rows': PER_QUERY, 'page': 1, 'output': 'json'}, doseq=True)
    return get('https://archive.org/advancedsearch.php?' + params)['response']['docs']

def inspect(doc):
    ident = scalar(doc.get('identifier')).strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}', ident):
        return 'bad_identifier', None
    data = get('https://archive.org/metadata/' + urllib.parse.quote(ident))
    md = data.get('metadata') or {}
    if scalar(md.get('mediatype')).lower() != 'movies':
        return 'not_movie', None
    lic = scalar(md.get('licenseurl')).strip().split(' ')[0].rstrip('/')
    if not LICENSE.match(lic):
        return 'license_not_eligible', None
    files = []
    for f in data.get('files') or []:
        name = scalar(f.get('name'))
        if f.get('private') or f.get('is_dark') or '/' in name.lstrip('/') or not name.lower().endswith(VIDEO_EXT):
            continue
        try:
            size = int(f.get('size') or 0)
        except (ValueError, TypeError):
            size = 0
        if size >= 2_000_000:
            files.append((f.get('source') == 'original', size, name))
    if not files:
        return 'no_video_file', None
    files.sort(reverse=True)
    title = scalar(md.get('title') or doc.get('title') or ident).strip()
    if not title:
        return 'no_title', None
    lang = scalar(md.get('language') or doc.get('language'))
    subjects = scalar(md.get('subject') or doc.get('subject'))
    persian = bool(re.search(r'(persian|farsi|iran|afghan|dari|فارسی|ایران|افغان|دری|\bfas\b|\bper\b)', title + ' ' + lang + ' ' + subjects, re.I))
    url = 'https://archive.org/download/' + urllib.parse.quote(ident, safe='') + '/' + urllib.parse.quote(files[0][2], safe='')
    item = {
        'title': title[:200], 'url': url,
        'language': 'fa' if persian else lang[:60],
        'genre': 'Persian / Iranian' if persian else 'Archive',
        'poster': 'https://archive.org/services/img/' + urllib.parse.quote(ident, safe=''),
        'description': ('Declared reuse license: ' + lic)[:300],
        'source': 'Internet Archive (declared license; rights/playback unverified)',
        'license_url': lic,
        'license_page': 'https://archive.org/details/' + urllib.parse.quote(ident, safe='')
    }
    return 'accepted', item

def main():
    counters = collections.Counter()
    docs, seen = [], set()
    for i, query in enumerate(SEARCHES, 1):
        try:
            found = search(query)
            print(f'Search {i}: {len(found)} results', flush=True)
            counters['search_results'] += len(found)
            for d in found:
                ident = scalar(d.get('identifier'))
                if ident not in seen:
                    seen.add(ident)
                    docs.append(d)
        except Exception as e:
            counters['search_errors'] += 1
            print(f'Search {i} FAILED: {type(e).__name__}: {str(e)[:220]}', flush=True)
    print(f'Unique candidates: {len(docs)}', flush=True)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(inspect, d) for d in docs[:350]]
        for future in concurrent.futures.as_completed(futures):
            try:
                reason, item = future.result()
                counters[reason] += 1
                if item:
                    results.append(item)
            except Exception as e:
                counters['metadata_errors'] += 1
                if counters['metadata_errors'] <= 5:
                    print(f'Metadata error: {type(e).__name__}: {str(e)[:160]}', flush=True)
    results.sort(key=lambda x: (x['language'] != 'fa', x['title'].casefold()))
    results = results[:MAX_ITEMS]
    report = {'candidates': len(docs), 'accepted': len(results), 'persian_metadata': sum(x['language'] == 'fa' for x in results), 'counts': dict(counters), 'note': 'Archive license metadata is not independent proof of rights or playback. Verify before distribution.'}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('DISCOVERY REPORT:', json.dumps(report, ensure_ascii=False), flush=True)
    if not results:
        print('NO NEW FILMS: preserving previous nonempty catalog if present.', flush=True)
        if not OUT.exists():
            OUT.write_text('[]\n', encoding='utf-8')
        elif OUT.read_text(encoding='utf-8').strip() in ('', '[]'):
            OUT.write_text('[]\n', encoding='utf-8')
        if counters['search_errors'] == len(SEARCHES):
            raise SystemExit('All archive searches failed; see report.')
        return
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Wrote {len(results)} candidates, Persian-tagged {report["persian_metadata"]}. Playback not verified.', flush=True)

if __name__ == '__main__':
    main()
