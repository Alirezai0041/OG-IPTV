#!/usr/bin/env python3
"""Metadata-only discovery of language-matched, reuse-labelled Archive videos.
Not a rights audit, playback check, or a guarantee of movie/series classification.
"""
import collections
import concurrent.futures
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

UA = 'OG-IPTV-catalog-research/3.0 (metadata discovery)'
SEARCHES = [
    ('fa', 'mediatype:movies AND (language:persian OR language:farsi OR language:dari OR language:fas OR language:per)'),
    ('fa', 'mediatype:movies AND (title:persian OR title:iranian OR subject:persian OR subject:iranian)'),
    ('fr', 'mediatype:movies AND (language:french OR language:fre OR language:fra)'),
    ('fr', 'mediatype:movies AND (subject:french OR title:français OR title:francais)'),
]
LICENSE = re.compile(r'^https?://creativecommons\.org/(?:publicdomain/(?:zero|mark)/1\.0|licenses/(?:by|by-sa)/(?:2\.0|2\.5|3\.0|4\.0))(?:/|$)', re.I)
VIDEO_EXT = ('.mp4', '.webm', '.ogv', '.m4v')
EXCLUDE = re.compile(r'\b(?:lecture|seminar|conference|interview|tutorial|workshop|webinar|podcast|software freedom|speech recognition|cours|conférence|entretien|tutoriel|formation|atelier|séminaire)\b|سخنرانی|آموزش|همایش|کارگاه|کنفرانس', re.I)
SERIES = re.compile(r'\b(?:episode|ep\.?\s*\d+|season|series|s\d{1,2}e\d{1,2}|épisode|saison|série|قسمت|سریال|فصل)\b', re.I)
FA = re.compile(r'\b(?:persian|farsi|dari|fas|per|iranian|afghan)\b|فارسی|دری|ایرانی|افغان', re.I)
FR = re.compile(r'\b(?:french|français|francais|fra|fre)\b|فرانسوی', re.I)
DUB = re.compile(r'\b(?:dubbed|dubbing|doublage|doublé|dub)\b|دوبله', re.I)

def scalar(v):
    return ' '.join(str(x) for x in v[:8]) if isinstance(v, list) else str(v or '')

def get(url, limit=8_000_000):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('response too large')
    return json.loads(raw)

def search(query):
    args = urllib.parse.urlencode({'q': query, 'fl[]': ['identifier','title','language','subject'], 'rows': 100, 'output':'json'}, doseq=True)
    return get('https://archive.org/advancedsearch.php?' + args)['response']['docs']

def inspect(doc):
    ident = scalar(doc.get('identifier')).strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}', ident):
        return 'invalid_identifier', None
    data = get('https://archive.org/metadata/' + urllib.parse.quote(ident))
    md = data.get('metadata') or {}
    if scalar(md.get('mediatype')).lower() != 'movies':
        return 'wrong_media_type', None
    title = scalar(md.get('title') or doc.get('title')).strip()
    language = scalar(md.get('language') or doc.get('language'))
    subject = scalar(md.get('subject') or doc.get('subject'))
    description = scalar(md.get('description'))[:1000]
    if not title:
        return 'missing_title', None
    if EXCLUDE.search(title + ' ' + subject):
        return 'excluded_non_entertainment', None
    # Require language metadata, not merely a Persian/French word in the title.
    if FA.search(language):
        lang = 'fa'
    elif FR.search(language):
        lang = 'fr'
    else:
        return 'language_unverified', None
    license_url = scalar(md.get('licenseurl')).strip().split(' ')[0].rstrip('/')
    if not LICENSE.match(license_url):
        return 'license_not_eligible', None
    videos = []
    for f in data.get('files') or []:
        name = scalar(f.get('name'))
        if f.get('private') or f.get('is_dark') or '/' in name.lstrip('/') or not name.lower().endswith(VIDEO_EXT):
            continue
        try:
            size = int(f.get('size') or 0)
        except (ValueError, TypeError):
            size = 0
        if size >= 2_000_000:
            videos.append((f.get('source') == 'original', size, name))
    if not videos:
        return 'no_video_file', None
    videos.sort(reverse=True)
    kind = 'series' if SERIES.search(title + ' ' + subject) else 'movies'
    dubbed = bool(DUB.search(title + ' ' + subject + ' ' + description))
    item = {
        'title': title[:200],
        'url': 'https://archive.org/download/' + urllib.parse.quote(ident) + '/' + urllib.parse.quote(videos[0][2]),
        'language': lang,
        'genre': ('Persian / Dari' if lang == 'fa' else 'French') + (' / Dubbed (unverified)' if dubbed else ''),
        'poster': 'https://archive.org/services/img/' + urllib.parse.quote(ident),
        'description': 'Declared license: ' + license_url + '. Playback and redistribution rights unverified.',
        'source': 'Internet Archive (metadata only)',
        'license_url': license_url,
        'license_page': 'https://archive.org/details/' + urllib.parse.quote(ident),
    }
    return kind, item

def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def main():
    counts = collections.Counter()
    docs = {}
    for label, query in SEARCHES:
        try:
            found = search(query)
            counts['search_results'] += len(found)
            print(f'Search {label}: {len(found)} results', flush=True)
            for doc in found:
                ident = scalar(doc.get('identifier'))
                if ident:
                    docs.setdefault(ident, doc)
        except Exception as exc:
            counts['search_errors'] += 1
            print(f'Search ERROR: {type(exc).__name__}: {str(exc)[:180]}', flush=True)
    movies, series = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(inspect, doc) for doc in list(docs.values())[:400]]
        for future in concurrent.futures.as_completed(futures):
            try:
                reason, item = future.result()
                counts[reason] += 1
                if item:
                    (series if reason == 'series' else movies).append(item)
            except Exception as exc:
                counts['metadata_errors'] += 1
                if counts['metadata_errors'] <= 5:
                    print('Metadata ERROR:', type(exc).__name__, str(exc)[:160], flush=True)
    movies.sort(key=lambda x: (x['language'], x['title'].casefold()))
    series.sort(key=lambda x: (x['language'], x['title'].casefold()))
    report = {
        'version': 3, 'candidates': len(docs),
        'accepted_movies': len(movies), 'accepted_series_candidates': len(series),
        'persian_movies': sum(x['language'] == 'fa' for x in movies),
        'french_movies': sum(x['language'] == 'fr' for x in movies),
        'persian_series_candidates': sum(x['language'] == 'fa' for x in series),
        'french_series_candidates': sum(x['language'] == 'fr' for x in series),
        'counts': dict(counts),
        'note': 'Metadata-based language and series classification only; dubbing, rights, playable links and episodes NOT independently verified. Do not distribute without review.'
    }
    write_json('external_catalog_report.json', report)
    # Never replace a previously nonempty feed with an empty discovery run.
    for filename, items in [('external_movies.json', movies), ('external_series_candidates.json', series)]:
        if items:
            write_json(filename, items)
        elif not Path(filename).exists():
            write_json(filename, [])
    print('DISCOVERY REPORT:', json.dumps(report, ensure_ascii=False), flush=True)
    if counts['search_errors'] == len(SEARCHES):
        raise SystemExit('All Archive searches failed')

if __name__ == '__main__':
    main()
