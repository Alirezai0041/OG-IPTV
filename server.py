#!/usr/bin/env python3
"""OG IPTV 3.2 - public playlist validation and Xtream-compatible API."""
import os, re, json, time, hashlib, urllib.request, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

HOST=os.getenv('HOST','0.0.0.0')
PORT=int(os.getenv('PORT','8080'))
USER=os.getenv('IPTV_USER','ogdemo')
PASSWORD=os.getenv('IPTV_PASSWORD','change-me-now')
PUBLIC_URL=os.getenv('PUBLIC_URL','https://og-iptv.onrender.com').rstrip('/')
CACHE_SECONDS=int(os.getenv('CACHE_SECONDS','3600'))
MAX_CHECKS=max(1,int(os.getenv('MAX_CHECKS','80')))
WORKERS=max(1,min(int(os.getenv('CHECK_WORKERS','16')),20))
SOURCES={
 'IR':'https://iptv-org.github.io/iptv/countries/ir.m3u',
 'FR':'https://iptv-org.github.io/iptv/countries/fr.m3u',
 'GB':'https://iptv-org.github.io/iptv/countries/uk.m3u',
 'US':'https://iptv-org.github.io/iptv/countries/us.m3u',
 'DE':'https://iptv-org.github.io/iptv/countries/de.m3u',
 'TR':'https://iptv-org.github.io/iptv/countries/tr.m3u',
 'CH':'https://iptv-org.github.io/iptv/countries/ch.m3u',
 'FA':'https://iptv-org.github.io/iptv/languages/fas.m3u',
 'SP':'https://iptv-org.github.io/iptv/categories/sports.m3u',
 'MU':'https://iptv-org.github.io/iptv/categories/music.m3u',
 'MV':'https://iptv-org.github.io/iptv/categories/movies.m3u',
}
NAMES={'IR':'🇮🇷 IRAN','FR':'🇫🇷 FRANCE','GB':'🇬🇧 UNITED KINGDOM','US':'🇺🇸 USA','DE':'🇩🇪 GERMANY','TR':'🇹🇷 TURKEY','CH':'🇨🇭 SWITZERLAND','FA':'🌐 PERSIAN','SP':'⚽ SPORTS','MU':'🎵 MUSIC','MV':'🎬 MOVIES','AD':'🔞 ADULT 18+'}
KEYS=list(SOURCES)+['AD']
CATEGORIES=[{'category_id':str(i),'category_name':NAMES[k],'parent_id':0} for i,k in enumerate(KEYS,1)]
ADULT_PLAYLIST_URL=os.getenv('ADULT_PLAYLIST_URL','').strip()
CACHE={'at':0,'streams':[],'urls':{},'stats':{},'checking':False}
LOCK=Lock()

def fetch_text(url,limit=8000000):
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 OG-IPTV/3.1','Accept':'*/*'})
    with urllib.request.urlopen(req,timeout=15) as r:
        return r.read(limit).decode('utf-8','replace')

def parse_m3u(key,raw):
    info=''; result=[]; seen=set()
    for line in raw.splitlines():
        line=line.strip()
        if line.startswith('#EXTINF:'):
            info=line
        elif info and line.startswith(('http://','https://')):
            if line not in seen:
                seen.add(line)
                logo=re.search(r'tvg-logo="([^"]*)"',info)
                result.append((key,info.rsplit(',',1)[-1].strip() or 'Unnamed Channel',logo.group(1) if logo else '',line))
            info=''
    return result

def check(item):
    key,name,logo,url=item
    # HTTP validation is only a preliminary check, not a playback guarantee.
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Range':'bytes=0-4095','Accept':'*/*'})
        with urllib.request.urlopen(req,timeout=4) as r:
            ctype=r.headers.get('Content-Type','').lower()
            final=r.geturl().lower().split('?')[0]
            sample=r.read(4096)
            status=r.status
        if status not in (200,206) or not sample:
            return None
        looks_hls=('#extm3u' in sample[:200].decode('utf-8','ignore').lower())
        looks_media=any(x in ctype for x in ('video/','audio/','mpegurl','octet-stream','mp2t'))
        looks_file=final.endswith(('.m3u8','.mpd','.ts','.mp4'))
        if looks_hls or looks_media or looks_file:
            return item
    except Exception:
        pass
    return None

def load():
    with LOCK:
        if time.time()-CACHE['at']<CACHE_SECONDS:return
        CACHE['checking']=True
        try:
            sources=dict(SOURCES)
            if ADULT_PLAYLIST_URL.startswith(('https://','http://')):sources['AD']=ADULT_PLAYLIST_URL
            candidates=[]; stats={}
            for key,url in sources.items():
                try:
                    entries=parse_m3u(key,fetch_text(url))
                    stats[key]={'found':len(entries),'checked':0,'accepted':0}
                    candidates.extend(entries)
                except Exception as exc:
                    stats[key]={'found':0,'checked':0,'accepted':0,'error':str(exc)[:120]}
            groups={k:[] for k in KEYS}
            for item in candidates:groups[item[0]].append(item)
            selected=[]
            while len(selected)<MAX_CHECKS and any(groups.values()):
                for key in KEYS:
                    if groups[key] and len(selected)<MAX_CHECKS:selected.append(groups[key].pop(0))
            for key,_,_,_ in selected:stats[key]['checked']+=1
            good=[]
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for future in as_completed([pool.submit(check,item) for item in selected]):
                    result=future.result()
                    if result:good.append(result)
            good.sort(key=lambda x:(KEYS.index(x[0]),x[1].lower()))
            streams=[]; urls={}
            for key,name,logo,url in good:
                sid=(int(hashlib.sha256((key+'|'+url).encode()).hexdigest()[:8],16) & 0x7fffffff) or 1
                while sid in urls and urls[sid] != url:
                    sid=(sid % 0x7fffffff)+1
                stats[key]['accepted']+=1
                streams.append({'num':len(streams)+1,'name':name,'stream_type':'live','stream_id':sid,'stream_icon':logo,'epg_channel_id':'','added':'0','category_id':str(KEYS.index(key)+1),'custom_sid':'','tv_archive':0,'direct_source':url,'tv_archive_duration':0,'container_extension':'m3u8'})
                urls[sid]=url
            CACHE.update(at=time.time(),streams=streams,urls=urls,stats=stats)
            print('OG IPTV 3.2 checked',len(selected),'accepted',len(streams),flush=True)
        finally:
            CACHE['checking']=False

class Handler(BaseHTTPRequestHandler):
    def reply(self,obj,status=200):
        body=json.dumps(obj,ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)
    def do_HEAD(self):
        path=urllib.parse.urlsplit(self.path).path
        self.reply({'ok':True} if path in ('/','/health') else {'error':'Not found'},200 if path in ('/','/health') else 404)
    def do_GET(self):
        parsed=urllib.parse.urlsplit(self.path);path=parsed.path
        p=urllib.parse.parse_qs(parsed.query)
        get=lambda key:p.get(key,[''])[0]
        if path in ('/','/health'):
            return self.reply({'ok':True,'name':'OG IPTV','version':'3.2','streams_loaded':len(CACHE['streams']),'last_scan':int(CACHE['at']),'adult_playlist_configured':bool(ADULT_PLAYLIST_URL)})
        if path=='/diagnostics':
            if get('username')!=USER or get('password')!=PASSWORD:return self.reply({'error':'Unauthorized'},401)
            load()
            return self.reply({'version':'3.2','note':'HTTP probe only; playback not guaranteed','stats':CACHE['stats'],'accepted_total':len(CACHE['streams'])})
        # Smarters can request /USERNAME/PASSWORD/STREAM_ID (no /live prefix).
        # Also preserve the /live/USERNAME/PASSWORD/STREAM_ID.m3u8 route.
        parts=path.strip('/').split('/')
        if (len(parts)==3 or (len(parts)==4 and parts[0]=='live')):
            try:
                if len(parts)==3:
                    u,pw,filename=parts
                else:
                    _,u,pw,filename=parts
                if urllib.parse.unquote(u)!=USER or urllib.parse.unquote(pw)!=PASSWORD:
                    return self.reply({'error':'Unauthorized'},401)
                sid=int(filename.split('.')[0])
            except ValueError:
                return self.reply({'error':'Invalid stream URL'},400)
            load();target=CACHE['urls'].get(sid)
            if not target:return self.reply({'error':'Unknown stream'},404)
            self.send_response(302)
            self.send_header('Location',target)
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            return
        if path not in ('/player_api.php','/get.php'):return self.reply({'error':'Not found'},404)
        if get('username')!=USER or get('password')!=PASSWORD:return self.reply({'user_info':{'auth':0,'status':'Disabled'}},401)
        load()
        if path=='/get.php':
            lines=['#EXTM3U']
            for s in CACHE['streams']:
                group=CATEGORIES[int(s['category_id'])-1]['category_name']
                target=PUBLIC_URL+'/live/'+urllib.parse.quote(USER,safe='')+'/'+urllib.parse.quote(PASSWORD,safe='')+'/'+str(s['stream_id'])+'.m3u8'
                lines.extend([f'#EXTINF:-1 tvg-logo="{s["stream_icon"]}" group-title="{group}",{s["name"]}',target])
            body='\n'.join(lines).encode('utf-8')
            self.send_response(200);self.send_header('Content-Type','application/x-mpegURL');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
        action=get('action')
        if action=='get_live_categories':return self.reply(CATEGORIES)
        if action=='get_live_streams':return self.reply([s for s in CACHE['streams'] if not get('category_id') or s['category_id']==get('category_id')])
        if action in ('get_vod_categories','get_vod_streams','get_series_categories','get_series'):return self.reply([])
        if action in ('get_short_epg','get_simple_data_table'):return self.reply({'epg_listings':[]})
        public=urllib.parse.urlsplit(PUBLIC_URL);secure=public.scheme=='https'
        return self.reply({'user_info':{'username':USER,'password':PASSWORD,'auth':1,'status':'Active','exp_date':None,'is_trial':'0','active_cons':'0','max_connections':'1','allowed_output_formats':['m3u8']},'server_info':{'url':public.hostname,'port':str(public.port or (443 if secure else 80)),'https_port':'443' if secure else '', 'server_protocol':public.scheme,'timezone':'Europe/Zurich','timestamp_now':int(time.time())}})

if __name__=='__main__':
    print('OG IPTV v3.2 running on',HOST,PORT,flush=True)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
