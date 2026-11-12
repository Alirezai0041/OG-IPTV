#!/usr/bin/env python3
"""O'G IPTV demo: small Xtream-compatible API for public playlists."""
import os, re, json, time, hashlib, urllib.request, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Lock

HOST=os.getenv('HOST','0.0.0.0'); PORT=int(os.getenv('PORT','8080'))
USER=os.getenv('IPTV_USER','ogdemo'); PASSWORD=os.getenv('IPTV_PASSWORD','change-me-now')
COUNTRIES={'IR':'Iran','FR':'France','GB':'United Kingdom','US':'USA','DE':'Germany','TR':'Turkey','CH':'Switzerland'}
SOURCES={code:f'https://iptv-org.github.io/iptv/countries/{"uk" if code=="GB" else code.lower()}.m3u' for code in COUNTRIES}
SOURCES.update({'FA':'https://iptv-org.github.io/iptv/languages/fas.m3u','SP':'https://iptv-org.github.io/iptv/categories/sports.m3u','MU':'https://iptv-org.github.io/iptv/categories/music.m3u','MV':'https://iptv-org.github.io/iptv/categories/movies.m3u'})
NAMES={'FA':'Persian International','SP':'Sports Worldwide','MU':'Music Worldwide','MV':'Movies Worldwide'}
CATEGORIES=[{'category_id':str(i+1),'category_name':('🔞 Adult 18+ (no licensed source)' if k=='AD' else f'{"🇮🇷" if k=="IR" else "🇫🇷" if k=="FR" else "🇬🇧" if k=="GB" else "🇺🇸" if k=="US" else "🇩🇪" if k=="DE" else "🇹🇷" if k=="TR" else "🇨🇭" if k=="CH" else "🌐"} {COUNTRIES.get(k,NAMES.get(k,k))}'),'parent_id':0} for i,k in enumerate([*SOURCES,'AD'])]
CACHE={'at':0,'streams':[],'urls':{}}; LOCK=Lock()
def load():
    with LOCK:
        if time.time()-CACHE['at']<3600:return
        streams=[]; urls={}; seen=set()
        for i,(key,source) in enumerate(SOURCES.items(),1):
            try:
                req=urllib.request.Request(source,headers={'User-Agent':'OG-IPTV-Demo/1.0'})
                with urllib.request.urlopen(req,timeout=18) as r: raw=r.read(6_000_000).decode('utf-8','replace')
            except Exception as e:
                print('Could not load',key,str(e),flush=True);continue
            info=''
            for line in raw.splitlines():
                line=line.strip()
                if line.startswith('#EXTINF:'): info=line
                elif line.startswith('http') and info:
                    if (key,line) in seen:continue
                    seen.add((key,line))
                    name=info.rsplit(',',1)[-1].strip() or 'Unnamed Channel'
                    logo=re.search(r'tvg-logo="([^"]+)"',info)
                    uid=int(hashlib.sha256((key+'|'+line).encode()).hexdigest()[:12],16)
                    streams.append({'num':len(streams)+1,'name':name,'stream_type':'live','stream_id':uid,'stream_icon':logo.group(1) if logo else '', 'epg_channel_id':'','added':'0','category_id':str(i),'custom_sid':'','tv_archive':0,'direct_source':'','tv_archive_duration':0})
                    urls[uid]=line; info=''
        CACHE.update(at=time.time(),streams=streams,urls=urls)
class Handler(BaseHTTPRequestHandler):
    def send(self,obj,status=200,ctype='application/json'):
        data=json.dumps(obj,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type',ctype+'; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        u=urllib.parse.urlsplit(self.path);p=urllib.parse.parse_qs(u.query);get=lambda k:p.get(k,[''])[0]
        if u.path=='/health':return self.send({'ok':True,'name':'OG IPTV Demo'})
        if u.path.startswith('/live/'):
            try: _,user,pw,sid=u.path.strip('/').split('/'); sid=int(sid.split('.')[0])
            except: return self.send({'error':'Invalid URL'},400)
            if user!=USER or pw!=PASSWORD:return self.send({'error':'Unauthorized'},401)
            load();url=CACHE['urls'].get(sid)
            if not url:return self.send({'error':'Unknown stream'},404)
            self.send_response(302);self.send_header('Location',url);self.end_headers();return
        if u.path not in ('/player_api.php','/get.php'):return self.send({'error':'Not found'},404)
        if get('username')!=USER or get('password')!=PASSWORD:return self.send({'user_info':{'auth':0,'status':'Disabled'}},401)
        load()
        if u.path=='/get.php':
            host=self.headers.get('Host',f'localhost:{PORT}'); scheme='https' if self.headers.get('X-Forwarded-Proto')=='https' else 'http'
            lines=['#EXTM3U']
            for s in CACHE['streams']:
                group=next((c['category_name'] for c in CATEGORIES if c['category_id']==s['category_id']),'Other')
                lines.extend([f'#EXTINF:-1 tvg-logo="{s["stream_icon"]}" group-title="{group}",{s["name"]}',f'{scheme}://{host}/live/{USER}/{PASSWORD}/{s["stream_id"]}.m3u8'])
            data='\n'.join(lines).encode();self.send_response(200);self.send_header('Content-Type','application/x-mpegURL');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        action=get('action')
        if action=='get_live_categories':return self.send(CATEGORIES)
        if action=='get_live_streams':return self.send([s for s in CACHE['streams'] if not get('category_id') or s['category_id']==get('category_id')])
        if action in ('get_vod_categories','get_vod_streams','get_series_categories','get_series'):return self.send([])
        if action in ('get_short_epg','get_simple_data_table'):return self.send({'epg_listings':[]})
        return self.send({'user_info':{'username':USER,'password':PASSWORD,'auth':1,'status':'Active','exp_date':None,'is_trial':'1','active_cons':'0','max_connections':'1','allowed_output_formats':['m3u8']},'server_info':{'url':self.headers.get('Host',f'localhost:{PORT}').split(':')[0],'port':str(PORT),'server_protocol':'http','timezone':'Europe/Zurich','timestamp_now':int(time.time())}})
if __name__=='__main__':
    print(f'O\'G IPTV demo running on {HOST}:{PORT}',flush=True)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
