#!/usr/bin/env python3
"""OG IPTV 3.5 - incremental verified-HLS catalog and Xtream-compatible API.

Only public streams supplied by configured playlists are indexed. An HLS master
playlist's RESOLUTION is metadata, not a guarantee of successful video playback.
"""
import os
import re
import json
import time
import hashlib
import urllib.request
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import RLock, Thread

HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '8080'))
USER = os.getenv('IPTV_USER', 'ogdemo')
PASSWORD = os.getenv('IPTV_PASSWORD', 'change-me-now')
PUBLIC_URL = os.getenv('PUBLIC_URL', 'https://og-iptv.onrender.com').rstrip('/')
CACHE_SECONDS = max(600, int(os.getenv('CACHE_SECONDS', '21600')))
WORKERS = max(1, min(int(os.getenv('CHECK_WORKERS', '12')), 24))
MIN_HEIGHT = max(1080, int(os.getenv('MIN_HEIGHT', '1080')))
MAX_CHECKS = max(0, int(os.getenv('MAX_CHECKS', '0')))  # 0 = all
ADULT_PLAYLIST_URL = os.getenv('ADULT_PLAYLIST_URL', '').strip()
BASE = 'https://iptv-org.github.io/iptv/'
SOURCES = {'COUNTRIES': BASE + 'index.country.m3u', 'CATEGORIES': BASE + 'index.category.m3u'}
ADULT_NAME = '🔞 ADULT 18+'
LOCK = RLock()
STATE = {
    'at': 0, 'started_at': 0, 'checking': False, 'error': None,
    'total': 0, 'checked': 0, 'accepted_variants': 0,
    'streams': [], 'urls': {}, 'categories': [], 'stats': {}, 'category_labels': [ADULT_NAME],
}


def request(url, timeout=9, limit=16000000):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(limit), response.headers.get('Content-Type', ''), response.geturl()


def parse_playlist(raw, source):
    result = []
    info = ''
    for line in raw.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        if line.startswith('#EXTINF:'):
            info = line
        elif info and line.startswith(('https://', 'http://')):
            match = re.search(r'group-title="([^"]*)"', info)
            group = (match.group(1).strip() if match else '') or 'OTHER'
            logo = re.search(r'tvg-logo="([^"]*)"', info)
            name = info.rsplit(',', 1)[-1].strip() or 'Unnamed'
            result.append((source, group, name, logo.group(1) if logo else '', line))
            info = ''
    return result


def check(url):
    """Return master-playlist variants with declared resolution >= MIN_HEIGHT."""
    try:
        raw, _, final = request(url, timeout=8, limit=262144)
        text = raw.decode('utf-8', 'replace').lstrip('\ufeff \r\n')
        if not text.startswith('#EXTM3U'):
            return []
        lines = text.splitlines()
        variants = []
        for i, line in enumerate(lines):
            if not line.startswith('#EXT-X-STREAM-INF:'):
                continue
            match = re.search(r'RESOLUTION=(\d+)x(\d+)', line, re.I)
            if not match:
                continue
            width, height = int(match.group(1)), int(match.group(2))
            if height < MIN_HEIGHT:
                continue
            for candidate in lines[i + 1:]:
                candidate = candidate.strip()
                if candidate and not candidate.startswith('#'):
                    absolute = urllib.parse.urljoin(final, candidate)
                    if absolute.startswith(('https://', 'http://')):
                        variants.append((absolute, width, height))
                    break
        return variants
    except Exception:
        return []


def category_for(entry):
    source, group, *_ = entry
    if source == 'ADULT':
        return ADULT_NAME
    return ('🌍 ' if source == 'COUNTRIES' else '📺 ') + group


def sort_name(name):
    return re.sub(r'\s+(?:4k|uhd|fhd|hd|1080p|1440p)$', '', name, flags=re.I).casefold()


def stream_id(category, url, used):
    # 31-bit positive IDs avoid Smarters' 32-bit signed integer truncation.
    sid = (int(hashlib.sha256((category + '|' + url).encode()).hexdigest()[:8], 16) & 0x7fffffff) or 1
    while sid in used and used[sid] != url:
        sid = (sid % 0x7fffffff) + 1
    return sid


def publish(records, stats, checked, total, done=False, error=None):
    # Rebuild an immutable snapshot so HTTP handlers never see half-written data.
    with LOCK:
        labels = list(STATE['category_labels'])
    ids = {label: str(i + 1) for i, label in enumerate(labels)}
    categories = [{'category_id': ids[label], 'category_name': label, 'parent_id': 0} for label in labels]
    sorted_records = sorted(records.values(), key=lambda x: (x[0].casefold(), sort_name(x[1]), -x[5], x[1].casefold()))
    streams, urls = [], {}
    for category, name, logo, url, width, height, source in sorted_records:
        sid = stream_id(category, url, urls)
        label = '4K' if height >= 2160 else ('1440p' if height >= 1440 else '1080p')
        streams.append({
            'num': len(streams) + 1, 'name': f'{name} [{label}]',
            'stream_type': 'live', 'stream_id': sid, 'stream_icon': logo,
            'epg_channel_id': '', 'added': '0', 'category_id': ids[category],
            'custom_sid': '', 'tv_archive': 0, 'direct_source': '',
            'tv_archive_duration': 0, 'container_extension': 'm3u8',
        })
        urls[sid] = url
    with LOCK:
        STATE.update(streams=streams, urls=urls, categories=categories,
                     stats={k: dict(v) for k, v in stats.items()}, checked=checked,
                     total=total, accepted_variants=len(streams), error=error)
        if done:
            STATE['at'] = time.time()
            STATE['checking'] = False


def scan():
    with LOCK:
        if STATE['checking']:
            return
        STATE.update(checking=True, started_at=time.time(), checked=0, total=0,
                     error=None, stats={}, streams=[], urls={}, at=0)
        STATE['categories'] = [{'category_id': '1', 'category_name': ADULT_NAME, 'parent_id': 0}]
    stats = {}
    byurl = {}
    records = {}
    checked = 0
    total = 0
    try:
        sources = dict(SOURCES)
        if ADULT_PLAYLIST_URL.startswith(('https://', 'http://')):
            sources['ADULT'] = ADULT_PLAYLIST_URL
        for source, playlist_url in sources.items():
            try:
                raw, _, _ = request(playlist_url, timeout=25)
                entries = parse_playlist(raw, source)
                if source == 'ADULT':
                    entries = [('ADULT', ADULT_NAME, n, l, u) for _, _, n, l, u in entries]
                stats[source] = {'found': len(entries), 'checked': 0, 'accepted': 0}
                for entry in entries:
                    byurl.setdefault(entry[4], []).append(entry)
            except Exception as exc:
                stats[source] = {'found': 0, 'checked': 0, 'accepted': 0, 'error': str(exc)[:150]}
                print('Playlist error', source, repr(exc), flush=True)
        unique = list(byurl)
        if MAX_CHECKS:
            unique = unique[:MAX_CHECKS]
        total = len(unique)
        all_labels = sorted({category_for(entry) for members in byurl.values() for entry in members} - {ADULT_NAME}, key=str.casefold)
        with LOCK:
            STATE['category_labels'] = [ADULT_NAME] + all_labels
        publish(records, stats, checked, total)
        print('OG IPTV 3.5 candidates', total, 'workers', WORKERS, flush=True)
        # Keep only a bounded number of futures in flight. Publishing occurs
        # every 25 completions or 10 seconds, whichever happens first.
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            iterator = iter(unique)
            pending = {}
            def submit_one():
                try:
                    url = next(iterator)
                except StopIteration:
                    return False
                pending[executor.submit(check, url)] = url
                return True
            for _ in range(WORKERS * 2):
                if not submit_one():
                    break
            last_publish = time.monotonic()
            while pending:
                finished, _ = wait(pending, timeout=2, return_when=FIRST_COMPLETED)
                for future in finished:
                    url = pending.pop(future)
                    try:
                        variants = future.result()
                    except Exception:
                        variants = []
                    checked += 1
                    memberships = byurl[url]
                    for source in {entry[0] for entry in memberships}:
                        stats[source]['checked'] += 1
                    for entry in memberships:
                        category = category_for(entry)
                        source, _, name, logo, _ = entry
                        for variant, width, height in variants:
                            key = (category, name, variant)
                            if key not in records:
                                records[key] = (category, name, logo, variant, width, height, source)
                                stats[source]['accepted'] += 1
                    submit_one()
                if checked and (checked % 25 == 0 or time.monotonic() - last_publish >= 10):
                    publish(records, stats, checked, total)
                    print('OG IPTV 3.5 progress', checked, '/', total,
                          'streams', len(records), flush=True)
                    last_publish = time.monotonic()
        publish(records, stats, checked, total, done=True)
        print('OG IPTV 3.5 completed', checked, 'streams', len(records), flush=True)
    except Exception as exc:
        print('OG IPTV 3.5 scan error', repr(exc), flush=True)
        publish(records, stats, checked, total, done=True, error=str(exc)[:250])


class Handler(BaseHTTPRequestHandler):
    def reply(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_HEAD(self):
        self.reply({'ok': True})

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        get = lambda key: params.get(key, [''])[0]
        with LOCK:
            snapshot = dict(STATE)
        if path in ('/', '/health'):
            return self.reply({
                'ok': True, 'name': 'OG IPTV', 'version': '3.5',
                'streams_loaded': len(snapshot['streams']),
                'last_scan': int(snapshot['at']), 'scanning': snapshot['checking'],
                'scan_error': snapshot['error'],
                'scan_checked': snapshot['checked'], 'scan_total': snapshot['total'],
                'scan_remaining': max(0, snapshot['total'] - snapshot['checked']),
                'categories_loaded': len(snapshot['categories']),
                'adult_streams_loaded': sum(1 for s in snapshot['streams'] if s['category_id'] == '1'),
                'adult_playlist_configured': bool(ADULT_PLAYLIST_URL),
                'minimum_verified_height': MIN_HEIGHT,
            })
        if path == '/diagnostics':
            if get('username') != USER or get('password') != PASSWORD:
                return self.reply({'error': 'Unauthorized'}, 401)
            return self.reply({'version': '3.5', 'stats': snapshot['stats'],
                               'checked': snapshot['checked'], 'total': snapshot['total'],
                               'accepted_total': len(snapshot['streams']),
                               'scanning': snapshot['checking']})
        parts = path.strip('/').split('/')
        if len(parts) == 3 or (len(parts) == 4 and parts[0] == 'live'):
            try:
                if len(parts) == 3:
                    u, pw, filename = parts
                else:
                    _, u, pw, filename = parts
                if urllib.parse.unquote(u) != USER or urllib.parse.unquote(pw) != PASSWORD:
                    return self.reply({'error': 'Unauthorized'}, 401)
                sid = int(filename.split('.')[0])
            except ValueError:
                return self.reply({'error': 'Invalid stream'}, 400)
            target = snapshot['urls'].get(sid)
            if not target:
                return self.reply({'error': 'Unknown stream'}, 404)
            self.send_response(302)
            self.send_header('Location', target)
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        if path not in ('/player_api.php', '/get.php'):
            return self.reply({'error': 'Not found'}, 404)
        if get('username') != USER or get('password') != PASSWORD:
            return self.reply({'user_info': {'auth': 0, 'status': 'Disabled'}}, 401)
        if path == '/get.php':
            lines = ['#EXTM3U']
            categories = {c['category_id']: c['category_name'] for c in snapshot['categories']}
            for stream in snapshot['streams']:
                group = categories.get(stream['category_id'], 'OTHER')
                target = (PUBLIC_URL + '/live/' + urllib.parse.quote(USER, safe='') + '/' +
                          urllib.parse.quote(PASSWORD, safe='') + '/' + str(stream['stream_id']) + '.m3u8')
                lines.extend([f'#EXTINF:-1 tvg-logo="{stream["stream_icon"]}" group-title="{group}",{stream["name"]}', target])
            body = '\n'.join(lines).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-mpegURL')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        action = get('action')
        if action == 'get_live_categories':
            return self.reply(snapshot['categories'])
        if action == 'get_live_streams':
            return self.reply([s for s in snapshot['streams'] if not get('category_id') or s['category_id'] == get('category_id')])
        if action in ('get_vod_categories', 'get_vod_streams', 'get_series_categories', 'get_series'):
            return self.reply([])
        if action in ('get_short_epg', 'get_simple_data_table'):
            return self.reply({'epg_listings': []})
        public = urllib.parse.urlsplit(PUBLIC_URL)
        secure = public.scheme == 'https'
        return self.reply({
            'user_info': {'username': USER, 'password': PASSWORD, 'auth': 1,
                          'status': 'Active', 'exp_date': None, 'is_trial': '0',
                          'active_cons': '0', 'max_connections': '1',
                          'allowed_output_formats': ['m3u8']},
            'server_info': {'url': public.hostname,
                            'port': str(public.port or (443 if secure else 80)),
                            'https_port': '443' if secure else '',
                            'server_protocol': public.scheme, 'timezone': 'Europe/Zurich',
                            'timestamp_now': int(time.time())},
        })


if __name__ == '__main__':
    with LOCK:
        STATE['categories'] = [{'category_id': '1', 'category_name': ADULT_NAME, 'parent_id': 0}]
    Thread(target=scan, daemon=True).start()
    print('OG IPTV v3.5 running on', HOST, PORT, flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
