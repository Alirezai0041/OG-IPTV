#!/usr/bin/env python3
"""OG IPTV Source Analyzer v2: read-only Xtream metadata audit.

Only calls catalog/info API endpoints. Never requests media URLs or bypasses access controls.
No credentials, playback URLs, tokens, or full response bodies are written to reports.
"""
import os
import json
import re
import urllib.parse
import urllib.request
from collections import Counter

BASE = os.environ['XTREAM_URL'].rstrip('/')
USER = os.environ['XTREAM_USER']
PASSWORD = os.environ['XTREAM_PASSWORD']
MAX_SAMPLES = 3


def api(action, **kwargs):
    query = urllib.parse.urlencode({'username': USER, 'password': PASSWORD, 'action': action, **kwargs})
    req = urllib.request.Request(BASE + '/player_api.php?' + query,
                                 headers={'User-Agent': 'OG-IPTV-Audit/2.0'})
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def safe_error(exc):
    return type(exc).__name__  # no URLs or secret-bearing exception strings


def category_names(action):
    data = api(action)
    if not isinstance(data, list):
        return {}, 0
    return {str(x.get('category_id')): str(x.get('category_name', ''))[:100]
            for x in data if isinstance(x, dict)}, len(data)


def pick_samples(items, pattern):
    chosen = []
    for x in items:
        if isinstance(x, dict) and pattern.search(str(x.get('name', '') or x.get('title', ''))):
            chosen.append(x)
            if len(chosen) >= MAX_SAMPLES:
                break
    if not chosen:
        chosen = [x for x in items if isinstance(x, dict)][:MAX_SAMPLES]
    return chosen


def audit():
    report = {'tool': 'OG IPTV Source Analyzer v2', 'mode': 'read_only_metadata',
              'warning': 'Catalog metadata cannot prove a source is independent, licensed, or permanent.'}
    definitions = [
        ('live', 'get_live_streams', 'get_live_categories', re.compile(r'be\s*in\s*sport', re.I), 'get_live_info', 'stream_id'),
        ('movies', 'get_vod_streams', 'get_vod_categories', re.compile(r'^(FR|EN)\s*[-|]', re.I), 'get_vod_info', 'vod_id'),
        ('series', 'get_series', 'get_series_categories', re.compile(r'^FR\s*[-|]', re.I), 'get_series_info', 'series_id'),
    ]
    for section, list_action, cat_action, matcher, info_action, id_param in definitions:
        try:
            cats, cat_count = category_names(cat_action)
            entries = api(list_action)
            if not isinstance(entries, list):
                report[section] = {'error': 'unexpected_catalog_format'}
                continue
            samples = []
            for item in pick_samples(entries, matcher):
                identifier = item.get('stream_id') if section != 'series' else item.get('series_id')
                sample = {
                    'title': str(item.get('name') or item.get('title') or '')[:120],
                    'category': cats.get(str(item.get('category_id')), 'unknown'),
                    'catalog_fields': sorted(k for k in item.keys() if k in (
                        'stream_id', 'series_id', 'category_id', 'container_extension',
                        'stream_type', 'direct_source', 'added', 'release_date')),
                    'has_direct_source': bool(item.get('direct_source')),
                    'container_extension': str(item.get('container_extension') or '')[:12],
                }
                if identifier is not None:
                    try:
                        info = api(info_action, **{id_param: identifier})
                        if isinstance(info, dict):
                            sample['info_fields'] = sorted(k for k in info.keys() if k in (
                                'info', 'movie_data', 'episodes', 'seasons', 'stream_data'))
                            if section == 'series':
                                eps = info.get('episodes', {})
                                if isinstance(eps, dict):
                                    sample['season_count'] = len(eps)
                                    sample['episode_count'] = sum(len(v) for v in eps.values() if isinstance(v, list))
                            if section == 'movies':
                                movie_data = info.get('movie_data', {})
                                if isinstance(movie_data, dict):
                                    sample['movie_extension'] = str(movie_data.get('container_extension') or '')[:12]
                        else:
                            sample['info_result'] = 'unexpected_format'
                    except Exception as exc:
                        sample['info_error'] = safe_error(exc)
                samples.append(sample)
            report[section] = {
                'total': len(entries), 'categories': cat_count,
                'samples': samples,
                'direct_source_count': sum(bool(x.get('direct_source')) for x in entries if isinstance(x, dict)),
                'extensions': dict(Counter(str(x.get('container_extension') or 'unspecified')[:12]
                                           for x in entries if isinstance(x, dict)).most_common(12)),
            }
        except Exception as exc:
            report[section] = {'error': safe_error(exc)}
    return report


if __name__ == '__main__':
    report = audit()
    with open('og_source_report_v2.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print('Audit complete. See og_source_report_v2.json')
