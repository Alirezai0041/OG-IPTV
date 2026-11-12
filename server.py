#!/usr/bin/env python3
"""OG IPTV - Xtream-compatible public IPTV catalogue."""
import os
import re
import json
import time
import hashlib
import urllib.request
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Lock

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
USER = os.getenv("IPTV_USER", "ogdemo")
PASSWORD = os.getenv("IPTV_PASSWORD", "change-me-now")
PUBLIC_URL = os.getenv(
    "PUBLIC_URL", "https://og-iptv.onrender.com"
).rstrip("/")

COUNTRIES = {
    "IR": "Iran",
    "FR": "France",
    "GB": "United Kingdom",
    "US": "USA",
    "DE": "Germany",
    "TR": "Turkey",
    "CH": "Switzerland",
}

SOURCES = {
    code: (
        "https://iptv-org.github.io/iptv/countries/"
        + ("uk" if code == "GB" else code.lower())
        + ".m3u"
    )
    for code in COUNTRIES
}

SOURCES.update({
    "FA": "https://iptv-org.github.io/iptv/languages/fas.m3u",
    "SP": "https://iptv-org.github.io/iptv/categories/sports.m3u",
    "MU": "https://iptv-org.github.io/iptv/categories/music.m3u",
    "MV": "https://iptv-org.github.io/iptv/categories/movies.m3u",
})

NAMES = {
    "FA": "Persian International",
    "SP": "Sports Worldwide",
    "MU": "Music Worldwide",
    "MV": "Movies Worldwide",
}

FLAGS = {
    "IR": "🇮🇷",
    "FR": "🇫🇷",
    "GB": "🇬🇧",
    "US": "🇺🇸",
    "DE": "🇩🇪",
    "TR": "🇹🇷",
    "CH": "🇨🇭",
}

KEYS = list(SOURCES) + ["AD"]

CATEGORIES = []
for i, key in enumerate(KEYS, 1):
    if key == "AD":
        name = "🔞 ADULT 18+"
    else:
        name = (
            FLAGS.get(key, "🌐")
            + " "
            + COUNTRIES.get(key, NAMES.get(key, key))
        )
    CATEGORIES.append({
        "category_id": str(i),
        "category_name": name,
        "parent_id": 0,
    })

# Optional, authorized adult-channel playlist.
# Add its URL as ADULT_PLAYLIST_URL in Render Environment.
ADULT_PLAYLIST_URL = os.getenv("ADULT_PLAYLIST_URL", "").strip()

CACHE = {"at": 0, "streams": [], "urls": {}}
LOCK = Lock()


def load():
    with LOCK:
        if time.time() - CACHE["at"] < 3600:
            return

        streams = []
        urls = {}
        seen = set()

        all_sources = dict(SOURCES)
        if ADULT_PLAYLIST_URL.startswith(("https://", "http://")):
            all_sources["AD"] = ADULT_PLAYLIST_URL

        for key, source in all_sources.items():
            category_id = str(KEYS.index(key) + 1)

            try:
                req = urllib.request.Request(
                    source,
                    headers={"User-Agent": "OG-IPTV/2.0"}
                )
                with urllib.request.urlopen(req, timeout=18) as response:
                    raw = response.read(6_000_000).decode(
                        "utf-8", "replace"
                    )
            except Exception as exc:
                print("Playlist error:", key, str(exc), flush=True)
                continue

            info = ""

            for line in raw.splitlines():
                line = line.strip()

                if line.startswith("#EXTINF:"):
                    info = line
                    continue

                if not line.startswith(("https://", "http://")):
                    continue

                if not info:
                    continue

                if (key, line) in seen:
                    info = ""
                    continue

                seen.add((key, line))

                name = (
                    info.rsplit(",", 1)[-1].strip()
                    or "Unnamed Channel"
                )

                logo = re.search(r'tvg-logo="([^"]+)"', info)

                sid = int(
                    hashlib.sha256(
                        (key + "|" + line).encode()
                    ).hexdigest()[:12],
                    16
                )

                streams.append({
                    "num": len(streams) + 1,
                    "name": name,
                    "stream_type": "live",
                    "stream_id": sid,
                    "stream_icon": (
                        logo.group(1) if logo else ""
                    ),
                    "epg_channel_id": "",
                    "added": "0",
                    "category_id": category_id,
                    "custom_sid": "",
                    "tv_archive": 0,
                    "direct_source": "",
                    "tv_archive_duration": 0,
                    "container_extension": "m3u8",
                })

                urls[sid] = line
                info = ""

        if streams or not CACHE["streams"]:
            CACHE.update(
                at=time.time(),
                streams=streams,
                urls=urls
            )
        else:
            CACHE["at"] = time.time() - 3300


class Handler(BaseHTTPRequestHandler):

    def send_json(self, obj, status=200):
        data = json.dumps(
            obj, ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(url.query)

        def get(key):
            return params.get(key, [""])[0]

        if url.path == "/health":
            return self.send_json({
                "ok": True,
                "name": "OG IPTV",
                "version": "2.0",
                "streams_loaded": len(CACHE["streams"]),
            })

        if url.path.startswith("/live/"):
            try:
                parts = url.path.strip("/").split("/")
                if len(parts) != 4:
                    raise ValueError("Invalid path")

                _, user, password, filename = parts

                user = urllib.parse.unquote(user)
                password = urllib.parse.unquote(password)
                sid = int(filename.split(".")[0])

            except (ValueError, TypeError):
                return self.send_json(
                    {"error": "Invalid stream URL"}, 400
                )

            if user != USER or password != PASSWORD:
                return self.send_json(
                    {"error": "Unauthorized"}, 401
                )

            load()
            target = CACHE["urls"].get(sid)

            if not target:
                return self.send_json(
                    {"error": "Unknown stream"}, 404
                )

            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if url.path not in ("/player_api.php", "/get.php"):
            return self.send_json(
                {"error": "Not found"}, 404
            )

        if get("username") != USER or get("password") != PASSWORD:
            return self.send_json({
                "user_info": {
                    "auth": 0,
                    "status": "Disabled"
                }
            }, 401)

        load()

        if url.path == "/get.php":
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

            data = "\n".join(lines).encode("utf-8")

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/x-mpegURL; charset=utf-8"
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        action = get("action")

        if action == "get_live_categories":
            return self.send_json(CATEGORIES)

        if action == "get_live_streams":
            category_id = get("category_id")
            result = [
                stream for stream in CACHE["streams"]
                if not category_id
                or stream["category_id"] == category_id
            ]
            return self.send_json(result)

        if action in (
            "get_vod_categories",
            "get_vod_streams",
            "get_series_categories",
            "get_series",
        ):
            return self.send_json([])

        if action in (
            "get_short_epg",
            "get_simple_data_table",
        ):
            return self.send_json({"epg_listings": []})

        parsed = urllib.parse.urlsplit(PUBLIC_URL)
        secure = parsed.scheme == "https"

        return self.send_json({
            "user_info": {
                "username": USER,
                "password": PASSWORD,
                "auth": 1,
                "status": "Active",
                "exp_date": None,
                "is_trial": "0",
                "active_cons": "0",
                "max_connections": "1",
                "allowed_output_formats": ["m3u8"],
            },
            "server_info": {
                "url": parsed.hostname,
                "port": str(
                    parsed.port or (443 if secure else 80)
                ),
                "https_port": "443" if secure else "",
                "server_protocol": parsed.scheme,
                "timezone": "Europe/Zurich",
                "timestamp_now": int(time.time()),
            }
        })


if __name__ == "__main__":
    print(
        f"OG IPTV running on {HOST}:{PORT}",
        flush=True
    )
    ThreadingHTTPServer(
        (HOST, PORT), Handler
    ).serve_forever()
