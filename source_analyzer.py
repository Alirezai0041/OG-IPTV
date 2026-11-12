
#!/usr/bin/env python3
"""OG IPTV - Read-only Xtream source analyzer."""

import os
import json
import urllib.request
import urllib.parse
from collections import Counter

BASE = os.environ["XTREAM_URL"].rstrip("/")
USER = os.environ["XTREAM_USER"]
PASSWORD = os.environ["XTREAM_PASSWORD"]


def fetch(action):
    query = urllib.parse.urlencode({
        "username": USER,
        "password": PASSWORD,
        "action": action
    })

    url = BASE + "/player_api.php?" + query

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "OG-IPTV-Analyzer/1.0"}
    )

    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def analyze(items):
    if not isinstance(items, list):
        return {"error": "Unexpected API response"}

    hosts = Counter()
    examples = []

    for item in items:
        if not isinstance(item, dict):
            continue

        for field in ("direct_source", "stream_url", "url"):
            value = item.get(field)

            if isinstance(value, str) and value.startswith(
                ("http://", "https://")
            ):
                host = urllib.parse.urlsplit(value).hostname
                if host:
                    hosts[host] += 1

        if len(examples) < 5:
            examples.append({
                "name": item.get("name") or item.get("title"),
                "category_id": item.get("category_id"),
                "has_direct_source": bool(item.get("direct_source"))
            })

    return {
        "count": len(items),
        "metadata_hosts": dict(hosts),
        "examples": examples
    }


def main():
    actions = {
        "live_categories": "get_live_categories",
        "live": "get_live_streams",
        "movie_categories": "get_vod_categories",
        "movies": "get_vod_streams",
        "series_categories": "get_series_categories",
        "series": "get_series"
    }

    report = {
        "service": "OG IPTV Source Analyzer",
        "mode": "metadata_only"
    }

    for name, action in actions.items():
        try:
            report[name] = analyze(fetch(action))
        except Exception as error:
            report[name] = {
                "error": type(error).__name__
            }

    with open(
        "og_source_report.json",
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            report,
            file,
            ensure_ascii=False,
            indent=2
        )

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
