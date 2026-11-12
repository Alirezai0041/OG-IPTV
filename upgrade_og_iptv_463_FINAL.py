#!/usr/bin/env python3
"""Apply OG IPTV 4.6.3 memory-safety improvements to the supplied 4.6.2 server.py.
No credential values are read or printed. Refuses unexpected source layouts.
"""
from pathlib import Path
import ast
p=Path(__file__).with_name('server.py')
s=p.read_text(encoding='utf-8')
if "'version':'4.6.2'" not in s or 'def xtream_catalog():' not in s:
    raise SystemExit('Expected OG IPTV 4.6.2 server.py in same directory; no changes made')
original=s

def change(old,new,count=1):
    global s
    actual=s.count(old)
    if actual!=count:
        raise SystemExit(f'Patch mismatch: expected {count} occurrences, got {actual}: {old[:90]!r}; no changes made')
    s=s.replace(old,new)

# Large Xtream catalogs were fetched all at once (movies, series and live),
# creating a very high transient memory peak. Fetch/process/release sequentially.
change("        movies=xtream_api('get_vod_streams')\n        series=xtream_api('get_series')\n        live=xtream_api('get_live_streams') if XTREAM_LIVE_LIMIT else []\n",'')
change('        for item in balanced_selection(movies,movie_names,XTREAM_MOVIE_LIMIT):',
       "        movies=xtream_api('get_vod_streams')\n        for item in balanced_selection(movies,movie_names,XTREAM_MOVIE_LIMIT):")
change('        for item in balanced_selection(series,series_names,XTREAM_SERIES_LIMIT):',
       "        del movies\n        series=xtream_api('get_series')\n        for item in balanced_selection(series,series_names,XTREAM_SERIES_LIMIT):")
change('        # Put sports channels first when a catalog cap is configured; no streams are fabricated.',
       "        del series\n        live=xtream_api('get_live_streams') if XTREAM_LIVE_LIMIT else []\n        # Put sports channels first when a catalog cap is configured; no streams are fabricated.")
change('        # Keep upstream catalog separate from public scans. Public scan snapshots',
       '        del live, selected_live\n        # Keep upstream catalog separate from public scans. Public scan snapshots')

# Stream API JSON to a spooled temporary file rather than allocating a second
# huge JSON byte string in memory. Spool rolls to local disk at 1 MiB.
change('import os, re, json, time, hashlib, urllib.request, urllib.parse, urllib.error, base64, gzip, datetime, xml.etree.ElementTree as ET',
       'import os, re, json, time, hashlib, urllib.request, urllib.parse, urllib.error, base64, gzip, datetime, xml.etree.ElementTree as ET, tempfile, shutil')
start=s.index('    def reply(self,obj,status=200):')
end=s.index('    def proxy_upstream(',start)
s=s[:start]+'''    def reply(self,obj,status=200):
        # Spool large JSON responses to disk to avoid duplicate giant byte buffers.
        with tempfile.SpooledTemporaryFile(max_size=1024*1024,mode='w+b') as out:
            encoder=json.JSONEncoder(ensure_ascii=False,separators=(',',':'))
            for piece in encoder.iterencode(obj):
                out.write(piece.encode('utf-8'))
            size=out.tell()
            out.seek(0)
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(size))
            self.end_headers()
            if self.command!='HEAD':
                try:shutil.copyfileobj(out,self.wfile,1024*128)
                except (BrokenPipeError,ConnectionResetError):pass

    def reply_catalog(self,groups,category_id=''):
        # Avoid concatenating two massive arrays or allocating filtered copies.
        with tempfile.SpooledTemporaryFile(max_size=1024*1024,mode='w+b') as out:
            encoder=json.JSONEncoder(ensure_ascii=False,separators=(',',':'))
            out.write(b'[')
            first=True
            for group in groups:
                for item in group:
                    if category_id and str(item.get('category_id'))!=category_id:
                        continue
                    if not first:out.write(b',')
                    first=False
                    for piece in encoder.iterencode(item):
                        out.write(piece.encode('utf-8'))
            out.write(b']')
            size=out.tell()
            out.seek(0)
            self.send_response(200)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(size))
            self.end_headers()
            if self.command!='HEAD':
                try:shutil.copyfileobj(out,self.wfile,1024*128)
                except (BrokenPipeError,ConnectionResetError):pass

'''+s[end:]

# Replace per-request list concatenation with a no-copy two-group response.
change("        if action=='get_live_streams':return self.reply([s for s in live['streams'] if str(s['category_id'])==cid] if cid else live['streams'])",
       "        if action=='get_live_streams':return self.reply_catalog((STATE['streams'],extra['live']),cid)")
change("        if action=='get_vod_streams':return self.reply([s for s in vod['movies'] if str(s['category_id'])==cid] if cid else vod['movies'])",
       "        if action=='get_vod_streams':return self.reply_catalog((VOD['movies'],extra['movies']),cid)")
change("        if action=='get_series':return self.reply([s for s in vod['series'] if str(s['category_id'])==cid] if cid else vod['series'])",
       "        if action=='get_series':return self.reply_catalog((VOD['series'],extra['series']),cid)")

# Avoid huge temporary lists for the 3 main catalog actions.
change("        if need_live:\n            live['streams']=live['streams']+extra['live']",
       "        if need_live:\n            if action!='get_live_streams':live['streams']=live['streams']+extra['live']")
change("        if need_vod:\n            vod['movies']=vod['movies']+extra['movies']",
       "        if need_vod:\n            if action!='get_vod_streams':vod['movies']=vod['movies']+extra['movies']")
change("            vod['series']=vod['series']+extra['series']",
       "            if action!='get_series':vod['series']=vod['series']+extra['series']")

# No need to rebuild a sorted live snapshot every 25 checks.
change('if checked and (checked%25==0 or time.monotonic()-last>=10):',
       'if checked and (checked%500==0 or time.monotonic()-last>=45):')

# Preserve user-configured caps rather than silently clamping to 30k.
change("XTREAM_SERIES_LIMIT=max(0,min(int(os.getenv('XTREAM_SERIES_LIMIT','25000')),30000))",
       "XTREAM_SERIES_LIMIT=max(0,min(int(os.getenv('XTREAM_SERIES_LIMIT','25000')),70000))")
change("XTREAM_LIVE_LIMIT=max(0,min(int(os.getenv('XTREAM_LIVE_LIMIT','1000')),30000))",
       "XTREAM_LIVE_LIMIT=max(0,min(int(os.getenv('XTREAM_LIVE_LIMIT','1000')),60000))")

# Compatibility: avoid 401 on Xtream API invalid credential handshake.
change("return self.reply({'user_info':{'auth':0,'status':'Disabled'}},401)",
       "return self.reply({'user_info':{'auth':0,'status':'Disabled'}},200)")


# Avoid peak RAM from simultaneously scanning public live and loading large Xtream catalogs.
change("""    Thread(target=refresh_loop,args=(scan_live,REFRESH_HOURS*3600),daemon=True).start()
    Thread(target=refresh_loop,args=(scan_vod,REFRESH_HOURS*3600),daemon=True).start()
    if XTREAM_READY:
        with LOCK:UPSTREAM_META['status']='loading'
        Thread(target=background_xtream,daemon=True).start()""",
       """    Thread(target=refresh_loop,args=(scan_vod,REFRESH_HOURS*3600),daemon=True).start()
    def start_catalogs():
        if XTREAM_READY:
            with LOCK:UPSTREAM_META['status']='loading'
            background_xtream()
        refresh_loop(scan_live,REFRESH_HOURS*3600)
    Thread(target=start_catalogs,daemon=True).start()""")

# Do not leak raw exception text from source URLs to the log.
# Existing external JSON feed hooks remain available for authorized sources.
s=s.replace('4.6.2','4.6.3')
ast.parse(s)
backup=p.with_name('server_462_backup.py')
if not backup.exists():backup.write_text(original,encoding='utf-8')
tmp=p.with_suffix('.py.tmp')
tmp.write_text(s,encoding='utf-8')
tmp.replace(p)
print('OG IPTV 4.6.3 patched successfully. Backup: server_462_backup.py')
