"""Shows which region's schedule the timers page is serving and whether the bot accepts it.

Run on the server:  docker compose exec aion2-checklist python inspect_timers.py
"""

import re
import sys
import urllib.request
from html.parser import HTMLParser

import config

URL = sys.argv[1] if len(sys.argv) > 1 else config.TIMERS_URL
req = urllib.request.Request(URL, headers={"User-Agent": config.USER_AGENT, "Accept": "text/html"})
html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
print(f"Fetched {URL} ({len(html)} bytes)\n")
try:
    import timers

    region, offset = timers.parse_page_meta(html)
    print(f"Region the page shows: {region!r}, schedule clock offset: {offset} minutes from UTC")
    print(f"Bot wants: {config.TIMERS_REGION!r} -> {'OK' if timers.page_accepted(region) else 'MISMATCH, the bot would ignore this page'}")
    print(f"Schedule rows found: {len(timers.parse_table(html))}\n")
except Exception as e:  # the checks below still run
    print("Could not run the bot's own parser:", e, "\n")


class P(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.selects, self.found, self.text_hits = [], [], [], []
        self.cur_select = None
        self.in_option = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "select":
            self.cur_select = {"attrs": a, "options": []}
            self.selects.append(self.cur_select)
        elif tag == "option" and self.cur_select is not None:
            self.in_option = {"value": a.get("value"), "selected": "selected" in a, "text": ""}
            self.cur_select["options"].append(self.in_option)
        for k, v in a.items():
            if v and re.search(r"region|server|global|korea|taiwan|version|realm", f"{k} {v}", re.I) and tag != "script":
                self.found.append(f"<{tag} {k}={v[:120]!r}>")

    def handle_endtag(self, tag):
        if tag == "select":
            self.cur_select = None
        if tag == "option":
            self.in_option = None

    def handle_data(self, data):
        if self.in_option is not None:
            self.in_option["text"] += data.strip()
        elif re.search(r"\b(global|korea|taiwan|region|server)\b", data, re.I) and len(data.strip()) < 120:
            self.text_hits.append(data.strip())


p = P()
p.feed(html)
print(f"== <select> dropdowns: {len(p.selects)}")
for s in p.selects:
    print("  attrs:", {k: v for k, v in s["attrs"].items() if k in ("id", "name", "class", "onchange", "data-url")})
    for o in s["options"]:
        print(f"    {'*' if o['selected'] else ' '} value={o['value']!r} text={o['text']!r}")
print("\n== attributes mentioning region/server/global/version:")
for f in dict.fromkeys(p.found):
    print("  ", f)
print("\n== short text mentioning global/korea/taiwan/region/server:")
for t in dict.fromkeys(p.text_hits):
    print("  ", t)
print("\n== script lines mentioning region/server/api/fetch:")
for line in re.findall(r"[^\n;]{0,100}(?:region|server=|serverId|/api/|fetch\()[^\n;]{0,140}", html, re.I)[:25]:
    print("  ", line.strip()[:240])
print("\n== links with query strings or region-looking paths:")
for href in dict.fromkeys(re.findall(r'href="([^"]*(?:region|server|global|\?)[^"]*)"', html, re.I)):
    print("  ", href[:200])
print("\n== table headers:")
print("  ", re.findall(r"<th[^>]*>(.*?)</th>", html, re.S)[:8])
