#!/usr/bin/env python3
"""OG IPTV v2.1 - Xtream compatible IPTV server."""

import os
import re
import json
import time
import hashlib
import urllib.request
import urllib.parse
from threading import Lock
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

USER = os.getenv("IPTV_USER", "ogdemo")
PASSWORD = os.getenv("IPTV_PASSWORD", "change-me-now")

PUBLIC_URL = os.getenv(
    "PUBLIC_URL",
    "https://og-iptv.onrender.com"
).rstrip("/")

CACHE_SECONDS = 3600

SOURCES = {
    "IR": "https://iptv-org.github.io/iptv/countries/ir.m3u",
    "FR": "https://iptv-org.github.io/iptv/countries/fr.m3u",
    "GB": "https://iptv-org.github.io/iptv/countries/uk.m3u",
    "US": "https://iptv-org.github.io/iptv/countries/us.m3u",
    "DE": "https://iptv-org.github.io/iptv/countries/de.m3u",
    "TR": "https://iptv-org.github.io/iptv/countries/tr.m3u",
    "CH": "https://iptv-org.github.io/iptv/countries/ch.m3u",
    "FA": "https://iptv-org.github.io/iptv/languages/fas.m3u",
    "SP": "https://iptv-org.github.io/iptv/categories/sports.m3u",
    "MU": "https://iptv-org.github.io/iptv/categories/music.m3u",
    "MV": "https://iptv-org.github.io/iptv/categories/movies.m3u",
}

ADULT_PLAYLIST_URL = os.getenv(
    "ADULT_PLAYLIST_URL", ""
).strip()

KEYS = list(SOURCES) + ["AD"]

NAMES = {
    "IR": "🇮🇷 IRAN",
    "FR": "🇫🇷 FRANCE",
    "GB": "🇬🇧 UNITED KINGDOM",
    "US": "🇺🇸 USA",
    "DE": "🇩🇪 GERMANY",
    "TR": "🇹🇷 TURKEY",
    "CH": "🇨🇭 SWITZERLAND",
    "FA": "🌐 PERSIAN",
    "SP": "⚽ SPORTS",
    "MU": "🎵 MUSIC",
    "MV": "🎬 MOVIES",
    "AD": "🔞 ADULT 18+",
}

CATEGORIES = [
    {
        "category_id": str(index),
        "category_name": NAMES[key],
        "parent_id": 0
    }
    for index, key in enumerate(KEYS, 1)
]

CACHE = {
    "at": 0,
    "streams": [],
    "urls": {}
}

LOCK = Lock()


def fetch_playlist(source):
    request = urllib.request.Request(
        source,
        headers={
            "User-Agent": "Mozilla/5.0 OG-IPTV/2.1"
        }
    )

    with urllib.request.urlopen(
        request, timeout=20
    ) as response:
        return response.read(8000000).decode(
            "utf-8", "replace"
        )


def load_streams():
    with LOCK:
        if time.time() - CACHE["at"] < CACHE_SECONDS:
            return

        streams = []
        urls = {}
        seen = set()

        sources = dict(SOURCES)

        if ADULT_PLAYLIST_URL.startswith(
            ("https://", "http://")
        ):
            sources["AD"] = ADULT_PLAYLIST_URL

        for key, source in sources.items():
            category_id = str(KEYS.index(key) + 1)

            try:
                raw = fetch_playlist(source)
            except Exception as error:
                print(
                    "Playlist failed:",
                    key,
                    str(error),
                    flush=True
                )
                continue

            info = ""

            for line in raw.splitlines():
                line = line.strip()

                if line.startswith("#EXTINF:"):
                    info = line
                    continue

                if not info:
                    continue

                if not line.startswith(
                    ("https://", "http://")
                ):
                    continue

                if (key, line) in seen:
                    info = ""
                    continue

                seen.add((key, line))

                name = (
                    info.rsplit(",", 1)[-1].strip()
                    or "Unnamed Channel"
                )

                logo_match = re.search(
                    r'tvg-logo="([^"]*)"', info
                )

                logo = (
                    logo_match.group(1)
                    if logo_match else ""
                )

                stream_id = int(
                    hashlib.sha256(
                        (key + "|" + line).encode()
                    ).hexdigest()[:12],
                    16
                )

                streams.append({
                    "num": len(streams) + 1,
                    "name": name,
                    "stream_type": "live",
                    "stream_id": stream_id,
                    "stream_icon": logo,
                    "epg_channel_id": "",
                    "added": "0",
                    "category_id": category_id,
                    "custom_sid": "",
                    "tv_archive": 0,
                    "direct_source": line,
                    "tv_archive_duration": 0,
                    "container_extension": "m3u8"
                })

                urls[stream_id] = line
                info = ""

        if streams or not CACHE["streams"]:
            CACHE.update({
                "at": time.time(),
                "streams": streams,
                "urls": urls
            })
        else:
            CACHE["at"] = time.time() - 3300

        print(
            "Loaded streams:",
            len(CACHE["streams"]),
            flush=True
        )


class Handler(BaseHTTPRequestHandler):

    def json_response(self, data, status=200):
        body = json.dumps(
            data, ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header(
            "Content-Length", str(len(body))
        )
        self.send_header(
            "Cache-Control", "no-store"
        )
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        if self.path.split("?")[0] in ("/", "/health"):
            self.send_response(200)
        else:
            self.send_response(404)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        def get(name):
            return params.get(name, [""])[0]

        if path in ("/", "/health"):
            return self.json_response({
                "ok": True,
                "name": "OG IPTV",
                "version": "2.1",
                "streams_loaded": len(CACHE["streams"]),
                "adult_playlist_configured": bool(
                    ADULT_PLAYLIST_URL
                )
            })

        if path.startswith("/live/"):
            try:
                parts = path.strip("/").split("/")

                if len(parts) != 4:
                    raise ValueError("Invalid URL")

                _, username, password, filename = parts

                username = urllib.parse.unquote(username)
                password = urllib.parse.unquote(password)

                stream_id = int(filename.split(".")[0])

            except (ValueError, TypeError):
                return self.json_response(
                    {"error": "Invalid stream URL"},
                    400
                )

            if username != USER or password != PASSWORD:
                return self.json_response(
                    {"error": "Unauthorized"},
                    401
                )

            load_streams()
            target = CACHE["urls"].get(stream_id)

            if not target:
                return self.json_response(
                    {"error": "Stream not found"},
                    404
                )

            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if path not in ("/player_api.php", "/get.php"):
            return self.json_response(
                {"error": "Not found"},
                404
            )

        if get("username") != USER or get("password") != PASSWORD:
            return self.json_response({
                "user_info": {
                    "auth": 0,
                    "status": "Disabled"
                }
            }, 401)

        load_streams()

        if path == "/get.php":
            lines = ["#EXTM3U"]

            for stream in CACHE["streams"]:
                category = CATEGORIES[
                    int(stream["category_id"]) - 1
                ]["category_name"]

                stream_url = (
                    PUBLIC_URL
                    + "/live/"
                    + urllib.parse.quote(USER, safe="")
                    + "/"
                    + urllib.parse.quote(PASSWORD, safe="")
                    + "/"
                    + str(stream["stream_id"])
                    + ".m3u8"
                )

                lines.append(
                    '#EXTINF:-1 tvg-logo="'
                    + stream["stream_icon"]
                    + '" group-title="'
                    + category
                    + '",'
                    + stream["name"]
                )
                lines.append(stream_url)

            body = "\n".join(lines).encode("utf-8")

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/x-mpegURL"
            )
            self.send_header(
                "Content-Length", str(len(body))
            )
            self.end_headers()
            self.wfile.write(body)
            return

        action = get("action")

        if action == "get_live_categories":
            return self.json_response(CATEGORIES)

        if action == "get_live_streams":
            category_id = get("category_id")

            result = [
                stream for stream in CACHE["streams"]
                if not category_id
                or stream["category_id"] == category_id
            ]

            return self.json_response(result)

        if action in (
            "get_vod_categories",
            "get_vod_streams",
            "get_series_categories",
            "get_series"
        ):
            return self.json_response([])

        if action in (
            "get_short_epg",
            "get_simple_data_table"
        ):
            return self.json_response({
                "epg_listings": []
            })

        public = urllib.parse.urlsplit(PUBLIC_URL)
        is_https = public.scheme == "https"

        return self.json_response({
            "user_info": {
                "username": USER,
                "password": PASSWORD,
                "auth": 1,
                "status": "Active",
                "exp_date": None,
                "is_trial": "0",
                "active_cons": "0",
                "max_connections": "1",
                "allowed_output_formats": [
                    "m3u8"
                ]
            },
            "server_info": {
                "url": public.hostname,
                "port": str(
                    public.port or
                    (443 if is_https else 80)
                ),
                "https_port": "443" if is_https else "",
                "server_protocol": public.scheme,
                "timezone": "Europe/Zurich",
                "timestamp_now": int(time.time())
            }
        })


if __name__ == "__main__":
    print(
        f"OG IPTV v2.1 running on {HOST}:{PORT}",
        flush=True
    )

    ThreadingHTTPServer(
        (HOST, PORT),
        Handler
    ).serve_forever()
