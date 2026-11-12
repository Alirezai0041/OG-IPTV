#!/usr/bin/env python3
"""OG IPTV 3.4: global public playlists, verified HLS 1080p+ variants, Xtream API."""
import os,re,json,time,hashlib,urllib.request,urllib.parse
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor,as_completed
from threading import Lock,Thread

HOST=os.getenv('HOST','0.0.0.0');PORT=int(os.getenv('PORT','8080'))
USER=os.getenv('IPTV_USER','ogdemo');PASSWORD=os.getenv('IPTV_PASSWORD','change-me-now')
PUBLIC_URL=os.getenv('PUBLIC_URL','https://og-iptv.onrender.com').rstrip('/')
CACHE_SECONDS=int(os.getenv('CACHE_SECONDS','21600'))
WORKERS=max(1,min(int(os.getenv('CHECK_WORKERS','12')),24))
MIN_HEIGHT=max(1080,int(os.getenv('MIN_HEIGHT','1080')))
# 0 means inspect all available candidates; scans can take minutes on a free host.
MAX_CHECKS=max(0,int(os.getenv('MAX_CHECKS','0')))
ADULT_PLAYLIST_URL=os.getenv('ADULT_PLAYLIST_URL','').strip()
BASE='https://iptv-org.github.io/iptv/'
SOURCES={'COUNTRIES':BASE+'index.country.m3u','CATEGORIES':BASE+'index.category.m3u'}
CACHE={'at':0,'streams':[],'urls':{},'stats':{},'checking':False,'error':None}
LOCK=Lock()
CATEGORIES=[];CAT_IDS={};LAST_ERROR=''

def request(url,timeout=9,limit=16000000):
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'*/*'})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return r.read(limit),r.headers.get('Content-Type',''),r.geturl()

def parse_playlist(raw,source):
    result=[];info=''
    for line in raw.decode('utf-8','replace').splitlines():
        line=line.strip()
        if line.startswith('#EXTINF:'):info=line
        elif info and line.startswith(('https://','http://')):
            m=re.search(r'group-title="([^"]*)"',info)
            group=(m.group(1).strip() if m else '') or 'OTHER'
            name=info.rsplit(',',1)[-1].strip() or 'Unnamed'
            logo=re.search(r'tvg-logo="([^"]*)"',info)
            result.append((source,group,name,logo.group(1) if logo else '',line))
            info=''
    return result

def variant_urls(url,data):
    text=data.decode('utf-8','replace')
    if not text.lstrip('\ufeff \r\n').startswith('#EXTM3U'):return []
    lines=text.splitlines();out=[]
    for i,line in enumerate(lines):
        if not line.startswith('#EXT-X-STREAM-INF:'):continue
        m=re.search(r'RESOLUTION=(\d+)x(\d+)',line,re.I)
        if not m or int(m.group(2))<MIN_HEIGHT:continue
        for nextline in lines[i+1:]:
            nextline=nextline.strip()
            if nextline and not nextline.startswith('#'):
                out.append((int(m.group(1)),int(m.group(2)),urllib.parse.urljoin(url,nextline)))
                break
    return out

def check(entry):
    source,group,name,logo,url=entry
    try:
        raw,ctype,final=request(url,timeout=8,limit=262144)
        variants=variant_urls(final,raw)
        # Media playlists without resolution information are NOT claimed as 1080p.
        return [(source,group,name,logo,u,w,h) for w,h,u in variants]
    except Exception:return []

def category_id(label):
    if label not in CAT_IDS:
        cid=str(len(CAT_IDS)+1);CAT_IDS[label]=cid
        CATEGORIES.append({'category_id':cid,'category_name':label,'parent_id':0})
    return CAT_IDS[label]

def load(force=False):
    with LOCK:
        if not force and CACHE['at'] and time.time()-CACHE['at']<CACHE_SECONDS:return
        CACHE['checking']=True;CACHE['error']=None
        try:
            entries=[];stats={}
            for key,url in SOURCES.items():
                try:
                    raw,_,_=request(url,timeout=25)
                    found=parse_playlist(raw,key);entries.extend(found)
                    stats[key]={'found':len(found),'checked':0,'accepted':0}
                except Exception as e:stats[key]={'error':str(e)[:120]}
            if ADULT_PLAYLIST_URL.startswith(('http://','https://')):
                try:
                    raw,_,_=request(ADULT_PLAYLIST_URL,timeout=25)
                    adult=parse_playlist(raw,'ADULT')
                    entries.extend([('ADULT','🔞 ADULT 18+',n,l,u) for _,_,n,l,u in adult])
                    stats['ADULT']={'found':len(adult),'checked':0,'accepted':0}
                except Exception as e:stats['ADULT']={'error':str(e)[:120]}
            # One probe per unique source URL; preserve memberships across countries/categories.
            byurl={}
            for entry in entries:byurl.setdefault(entry[4],[]).append(entry)
            unique=list(byurl)
            if MAX_CHECKS:unique=unique[:MAX_CHECKS]
            checked=set(unique)
            for url in checked:
                for source,*_ in byurl[url]:
                    if 'checked' in stats.get(source,{}):stats[source]['checked']+=1
            good={}
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures={pool.submit(check,byurl[url][0]):url for url in unique}
                for f in as_completed(futures):
                    url=futures[f]
                    try:variants=f.result()
                    except Exception:variants=[]
                    if variants:good[url]=variants
            # Build all memberships; a channel can appear in both country and sports.
            streams=[];urls={};CAT_IDS.clear();CATEGORIES.clear();seen=set()
            # Always expose the adult category, even if no authorized source is configured.
            category_id('🔞 ADULT 18+')
            records=[]
            for url,variants in good.items():
                for source,group,name,logo,_ in byurl[url]:
                    if source=='COUNTRIES':category='🌍 '+group
                    elif source=='CATEGORIES':category='📺 '+group
                    else:category='🔞 ADULT 18+'
                    for _,_,_,_,variant,w,h in variants:
                        label='4K' if h>=2160 else ('1440p' if h>=1440 else '1080p')
                        key=(category,name,variant)
                        if key not in seen:
                            seen.add(key);records.append((category,name,logo,variant,w,h,label,source))
            records.sort(key=lambda x:(x[0].casefold(),re.sub(r'\s+(?:4k|uhd|fhd|hd|1080p|1440p)$','',x[1],flags=re.I).casefold(),x[1].casefold(),-x[5]))
            for category,name,logo,url,w,h,label,source in records:
                cid=category_id(category)
                sid=(int(hashlib.sha256((category+'|'+url).encode()).hexdigest()[:8],16)&0x7fffffff) or 1
                while sid in urls and urls[sid]!=url:sid=(sid%0x7fffffff)+1
                display=f'{name} [{label}]'
                streams.append({'num':len(streams)+1,'name':display,'stream_type':'live','stream_id':sid,'stream_icon':logo,'epg_channel_id':'','added':'0','category_id':cid,'custom_sid':'','tv_archive':0,'direct_source':url,'tv_archive_duration':0,'container_extension':'m3u8'})
                urls[sid]=url
                stats[source]['accepted']+=1
            CACHE.update(at=time.time(),streams=streams,urls=urls,stats=stats,error=None)
            print('OG IPTV 3.4 checked',len(unique),'accepted',len(streams),flush=True)
        except Exception as e:
            CACHE['error']=str(e);print('Scan error',repr(e),flush=True)
        finally:CACHE['checking']=False

class Handler(BaseHTTPRequestHandler):
    def reply(self,obj,status=200):
        body=json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)
    def do_HEAD(self):self.reply({'ok':True})
    def do_GET(self):
        parsed=urllib.parse.urlsplit(self.path);path=parsed.path
        p=urllib.parse.parse_qs(parsed.query);get=lambda key:p.get(key,[''])[0]
        if path in ('/','/health'):
            return self.reply({'ok':True,'name':'OG IPTV','version':'3.4','streams_loaded':len(CACHE['streams']),'last_scan':int(CACHE['at']),'scanning':CACHE['checking'],'scan_error':CACHE['error'],'adult_playlist_configured':bool(ADULT_PLAYLIST_URL),'minimum_verified_height':MIN_HEIGHT})
        if path=='/diagnostics':
            if get('username')!=USER or get('password')!=PASSWORD:return self.reply({'error':'Unauthorized'},401)
            return self.reply({'version':'3.4','stats':CACHE['stats'],'accepted_total':len(CACHE['streams']),'scanning':CACHE['checking']})
        parts=path.strip('/').split('/')
        if len(parts)==3 or (len(parts)==4 and parts[0]=='live'):
            try:
                if len(parts)==3:u,pw,filename=parts
                else:_,u,pw,filename=parts
                if urllib.parse.unquote(u)!=USER or urllib.parse.unquote(pw)!=PASSWORD:return self.reply({'error':'Unauthorized'},401)
                sid=int(filename.split('.')[0])
            except ValueError:return self.reply({'error':'Invalid stream'},400)
            target=CACHE['urls'].get(sid)
            if not target:return self.reply({'error':'Unknown stream'},404)
            self.send_response(302);self.send_header('Location',target);self.end_headers();return
        if path not in ('/player_api.php','/get.php'):return self.reply({'error':'Not found'},404)
        if get('username')!=USER or get('password')!=PASSWORD:return self.reply({'user_info':{'auth':0,'status':'Disabled'}},401)
        if path=='/get.php':
            lines=['#EXTM3U']
            for s in CACHE['streams']:
                group=CATEGORIES[int(s['category_id'])-1]['category_name']
                target=PUBLIC_URL+'/live/'+urllib.parse.quote(USER,safe='')+'/'+urllib.parse.quote(PASSWORD,safe='')+'/'+str(s['stream_id'])+'.m3u8'
                lines.extend([f'#EXTINF:-1 tvg-logo="{s["stream_icon"]}" group-title="{group}",{s["name"]}',target])
            body='\n'.join(lines).encode();self.send_response(200);self.send_header('Content-Type','application/x-mpegURL');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
        action=get('action')
        if action=='get_live_categories':return self.reply(CATEGORIES)
        if action=='get_live_streams':return self.reply([s for s in CACHE['streams'] if not get('category_id') or s['category_id']==get('category_id')])
        if action in ('get_vod_categories','get_vod_streams','get_series_categories','get_series'):return self.reply([])
        if action in ('get_short_epg','get_simple_data_table'):return self.reply({'epg_listings':[]})
        public=urllib.parse.urlsplit(PUBLIC_URL);secure=public.scheme=='https'
        return self.reply({'user_info':{'username':USER,'password':PASSWORD,'auth':1,'status':'Active','exp_date':None,'is_trial':'0','active_cons':'0','max_connections':'1','allowed_output_formats':['m3u8']},'server_info':{'url':public.hostname,'port':str(public.port or (443 if secure else 80)),'https_port':'443' if secure else '','server_protocol':public.scheme,'timezone':'Europe/Zurich','timestamp_now':int(time.time())}})

if __name__=='__main__':
    Thread(target=load,daemon=True).start()
    print('OG IPTV v3.4 running on',HOST,PORT,flush=True)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
