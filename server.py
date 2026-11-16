#!/usr/bin/env python3
"""OG IPTV 4.7.0 — Xtream-compatible, multi-source authorized IPTV catalog.

No external Python dependencies. Configure credentials and licensed sources in Render.
This service does not bypass access controls or supply copyrighted media.
"""
import os, re, json, time, hashlib, urllib.request, urllib.parse, urllib.error
import base64, gzip, datetime, xml.etree.ElementTree as ET, tempfile, shutil
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
from threading import RLock, Thread, BoundedSemaphore
from collections import defaultdict, deque

VERSION='4.9.0'
HOST=os.getenv('HOST','0.0.0.0'); PORT=int(os.getenv('PORT','8080'))
USER=os.getenv('IPTV_USER','ogdemo'); PASSWORD=os.getenv('IPTV_PASSWORD','change-me-now')
PUBLIC_URL=os.getenv('PUBLIC_URL','https://og-iptv.onrender.com').rstrip('/')
CACHE_SECONDS=max(600,int(os.getenv('CACHE_SECONDS','21600')))
WORKERS=max(1,min(int(os.getenv('CHECK_WORKERS','12')),24))
MIN_HEIGHT=max(0,int(os.getenv('MIN_HEIGHT','0')))
MAX_CHECKS=max(0,int(os.getenv('MAX_CHECKS','0')))
REFRESH_HOURS=max(1,min(int(os.getenv('REFRESH_HOURS','24')),168))
ADULT_ENABLED=os.getenv('ADULT_ENABLED','1')=='1'
ADULT_PLAYLIST_URL=os.getenv('ADULT_PLAYLIST_URL','').strip()
ADULT_PLAYLIST_URLS=os.getenv('ADULT_PLAYLIST_URLS','').strip()
EXTRA_LIVE_URLS=os.getenv('EXTRA_LIVE_URLS','').strip()
VOD_JSON_URL=os.getenv('VOD_JSON_URL','').strip()
SERIES_JSON_URL=os.getenv('SERIES_JSON_URL','').strip()
ADULT_MOVIES_JSON_URLS=os.getenv('ADULT_MOVIES_JSON_URLS','').strip()
ADULT_SERIES_JSON_URLS=os.getenv('ADULT_SERIES_JSON_URLS','').strip()
MOVIES_JSON_URLS=os.getenv('MOVIES_JSON_URLS','').strip()
SERIES_JSON_URLS=os.getenv('SERIES_JSON_URLS','').strip()
LICENSED_CATALOGS_URL=os.getenv('LICENSED_CATALOGS_URL','').strip()
ARCHIVE_DISCOVERY=os.getenv('ARCHIVE_DISCOVERY','1')=='1'
ADULT_ARCHIVE_DISCOVERY=os.getenv('ADULT_ARCHIVE_DISCOVERY','1')=='1'
ARCHIVE_MAX=max(0,min(int(os.getenv('ARCHIVE_MAX','60')),500))
PERSIAN_RESERVE_PERCENT=max(0,min(int(os.getenv('PERSIAN_RESERVE_PERCENT','55')),90))
XTREAM_URL=os.getenv('XTREAM_URL','').rstrip('/')
XTREAM_USER=os.getenv('XTREAM_USER',''); XTREAM_PASSWORD=os.getenv('XTREAM_PASSWORD','')
XTREAM_ENABLED=os.getenv('XTREAM_ENABLED','0')=='1'
XTREAM_READY=XTREAM_ENABLED and all((XTREAM_URL,XTREAM_USER,XTREAM_PASSWORD))
XTREAM_MOVIE_LIMIT=max(0,min(int(os.getenv('XTREAM_MOVIE_LIMIT','100000')),120000))
XTREAM_SERIES_LIMIT=max(0,min(int(os.getenv('XTREAM_SERIES_LIMIT','25000')),70000))
XTREAM_LIVE_LIMIT=max(0,min(int(os.getenv('XTREAM_LIVE_LIMIT','1000')),60000))
XMLTV_URL=os.getenv('XMLTV_URL','').strip()
EPG_CACHE_TTL=900
ADULT_NAME='🔞 ADULT 18+'
ADULT_MOVIE_NAME='🔞 فیلم‌های بزرگسالان 18+'
ADULT_SERIES_NAME='🔞 سریال‌های بزرگسالان 18+'
PERSIAN_DUB_TERMS=re.compile(r'(?:دوبله[‌\s-]*(?:فارسی|فارسي|پارسی|ایرانی)|مدبلج[‌\s-]*(?:فارسی|فارسي)|(?:persian|farsi)[\s_-]*dub(?:bed)?|dub(?:bed)?[\s_-]*(?:in[\s_-]*)?(?:persian|farsi)|fa[\s_-]*dub(?:bed)?)',re.I)
PERSIAN_ORIGINAL_TERMS=re.compile(r'(?:ایران|ایرانی|فارسی|فارسي|پارسی|iran(?:ian)?|farsi|persian|افلام[‌\s]+ايراني|أفلام[‌\s]+إيرانية)',re.I)
PERSIAN_VOD_TERMS=re.compile(r'(?:ایران|ایرانی|فارسی|فارسي|پارسی|دوبله|زیرنویس[‌\s-]*فارسی|iran(?:ian)?|farsi|persian|مدبلج[‌\s-]*فارسي)',re.I)
ADULT_TERMS=re.compile(r'(?:\bxxx\b|\b18\s*\+|\badult\b|\bporn\b|\berotic\b|\bnsfw\b|\badulte\b|\bporno\b|\bsexy\b|بزرگسال|للكبار|للبالغين)',re.I)
BEIN_TERMS=re.compile(r'(?:\bbe\s*in\s*(?:sports?|sport)?\b|\bbein\b|\bbein\s*max\b)',re.I)
BASE='https://iptv-org.github.io/iptv/'
SOURCES={'COUNTRIES':BASE+'index.country.m3u','CATEGORIES':BASE+'index.category.m3u','IRAN':BASE+'countries/ir.m3u'}
LOCK=RLock(); XMLTV_LOCK=RLock(); DETAIL_SEMAPHORE=BoundedSemaphore(5)
STATE={'at':0,'started_at':0,'checking':False,'error':None,'total':0,'checked':0,
       'streams':[],'urls':{},'categories':[{'category_id':'1','category_name':ADULT_NAME,'parent_id':0}],
       'stats':{},'category_labels':[ADULT_NAME],'verified_manifest':0,'verified_playback':0,'failed':0}
VOD={'checking':False,'at':0,'error':None,'movies':[],'movie_info':{},'movie_urls':{},
     'movie_categories':[],'series':[],'series_info':{},'series_urls':{},'series_categories':[],
     'found':0,'rejected':0,'sources':{}}
UPSTREAM={'live':{},'movie':{},'series':{}}
UPSTREAM_SERIES_IDS={}; UPSTREAM_MOVIE_IDS={}; UPSTREAM_LIVE_IDS={}
UPSTREAM_ROWS={'live':[],'live_categories':[],'movies':[],'movie_info':{},'movie_categories':[],
               'series':[],'series_info':{},'series_categories':[]}
UPSTREAM_META={'status':'disabled','movies':0,'series':0,'live':0,'error':None}
EPG_CACHE={}; XMLTV_STATE={'at':0,'channels':{},'error':None}

# Original ISO country labels are intentionally kept independent of upstream category names.
COUNTRY_NAME_TO_CODE={'afghanistan':'AF','iran':'IR','iran, islamic republic of':'IR','islamic republic of iran':'IR',
 'france':'FR','switzerland':'CH','germany':'DE','türkiye':'TR','turkey':'TR','united kingdom':'GB',
 'united states':'US','united states of america':'US','usa':'US','uk':'GB','england':'GB',
 'russia':'RU','russian federation':'RU','italy':'IT','iraq':'IQ','india':'IN','pakistan':'PK',
 'canada':'CA','australia':'AU','spain':'ES','portugal':'PT','netherlands':'NL','belgium':'BE',
 'austria':'AT','sweden':'SE','norway':'NO','denmark':'DK','finland':'FI','poland':'PL',
 'ukraine':'UA','china':'CN','japan':'JP','korea, republic of':'KR','south korea':'KR',
 'turkiye':'TR','israel':'IL','saudi arabia':'SA','united arab emirates':'AE',
 'egypt':'EG','morocco':'MA','algeria':'DZ','tunisia':'TN','brazil':'BR','argentina':'AR',
 'mexico':'MX','south africa':'ZA','new zealand':'NZ','international':'ZZ','kosovo':'XK'}
ISO_COUNTRY_CODES=set('AF IR FR CH DE TR GB US RU IT IQ IN PK CA AU ES PT NL BE AT SE NO DK FI PL UA CN JP KR IL SA AE EG MA DZ TN BR AR MX ZA NZ XK'.split())
LANGS={'fr':'FR','fra':'FR','french':'FR','français':'FR','en':'EN','eng':'EN','english':'EN',
 'fa':'FA','fas':'FA','per':'FA','persian':'FA','farsi':'FA','فارسی':'FA','de':'DE','ger':'DE',
 'german':'DE','es':'ES','spanish':'ES','tr':'TR','turkish':'TR','it':'IT','italian':'IT',
 'ar':'AR','arabic':'AR','hi':'HI','hindi':'HI','ru':'RU','russian':'RU','pt':'PT'}
GENRES={'action':'ACTION','comedy':'COMEDY','horror':'HORROR','drama':'DRAMA','romance':'ROMANCE',
 'thriller':'THRILLER','documentary':'DOCUMENTARY','animation':'ANIMATION','science fiction':'SCI-FI',
 'sci-fi':'SCI-FI','family':'FAMILY','adventure':'ADVENTURE','mystery':'MYSTERY',
 'fantasy':'FANTASY','music':'MUSIC','history':'HISTORY','اکشن':'ACTION','کمدی':'COMEDY',
 'ترسناک':'HORROR','درام':'DRAMA','عاشقانه':'ROMANCE'}

def valid_http(url):
    try:
        p=urllib.parse.urlsplit(str(url))
        return p.scheme in ('http','https') and bool(p.hostname) and not p.username and not p.password
    except Exception:return False

def url_list(value):
    return list(dict.fromkeys(u.strip() for u in re.split(r'[\n,;]+',value or '') if valid_http(u.strip())))

def request(url,timeout=9,limit=16000000):
    if not valid_http(url):raise ValueError('Invalid URL')
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 OG-IPTV/'+VERSION,'Accept':'*/*'})
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        raw=resp.read(limit+1)
        if len(raw)>limit:raise ValueError('Response exceeds configured limit')
        return raw,resp.headers.get('Content-Type',''),resp.geturl()

def get_json(url,timeout=12,limit=16000000):
    return json.loads(request(url,timeout,limit)[0].decode('utf-8-sig'))

def stable_id(seed):return (int(hashlib.sha256(seed.encode()).hexdigest()[:8],16)&0x7fffffff) or 1

def country_flag(code):
    code=str(code or '').upper()
    if code=='XK':return '🇽🇰'
    if len(code)!=2 or not code.isalpha():return ''
    return ''.join(chr(127397+ord(c)) for c in code)

def flag_label(label):
    name=str(label or '').strip()
    if not name:return 'OTHER'
    if any(0x1f1e6<=ord(c)<=0x1f1ff for c in name):return name
    plain=re.sub(r'^[^\w]+','',name).strip()
    norm=plain.casefold().replace('_',' ')
    m=re.match(r'^([A-Za-z]{2})(?=\s*[-|:]|\s|$)',plain)
    code=m.group(1).upper() if m and m.group(1).upper() in ISO_COUNTRY_CODES else None
    if not code:
        for country in sorted(COUNTRY_NAME_TO_CODE,key=len,reverse=True):
            if norm==country or norm.startswith(country+' ') or norm.startswith(country+' -'):
                code=COUNTRY_NAME_TO_CODE[country];break
    flag='🌍' if code=='ZZ' else country_flag(code)
    return flag+' '+name if flag else name

def parse_playlist(raw,source):
    result=[];info=''
    for line in raw.decode('utf-8-sig','replace').splitlines():
        line=line.strip()
        if line.startswith('#EXTINF:'):info=line
        elif info and line.startswith(('https://','http://')):
            group=re.search(r'group-title="([^"]*)"',info)
            logo=re.search(r'tvg-logo="([^"]*)"',info)
            epg=re.search(r'tvg-id="([^"]*)"',info)
            result.append((source,(group.group(1).strip() if group else '') or 'OTHER',
                           info.rsplit(',',1)[-1].strip() or 'Unnamed',logo.group(1) if logo else '',
                           line,epg.group(1) if epg else ''))
            info=''
    return result

def category_for(entry):
    if entry[0].startswith('ADULT'):return ADULT_NAME
    if ADULT_ENABLED and ADULT_TERMS.search(entry[1]+' '+entry[2]):return ADULT_NAME
    if entry[0] in ('COUNTRIES','IRAN'):return flag_label('Iran' if entry[0]=='IRAN' else entry[1])
    return '📺 '+entry[1]

def check_live(url):
    """Checks HLS manifest syntax only; not a decoded playback test."""
    try:
        raw,ctype,final=request(url,timeout=8,limit=262144)
        text=raw.decode('utf-8','replace').lstrip('\ufeff \r\n')
        if not text.startswith('#EXTM3U'):return []
        lines=text.splitlines();variants=[]
        for i,line in enumerate(lines):
            if not line.startswith('#EXT-X-STREAM-INF:'):continue
            m=re.search(r'RESOLUTION=(\d+)x(\d+)',line,re.I)
            w,h=(int(m.group(1)),int(m.group(2))) if m else (0,0)
            if h<MIN_HEIGHT:continue
            for candidate in lines[i+1:]:
                candidate=candidate.strip()
                if candidate and not candidate.startswith('#'):
                    absolute=urllib.parse.urljoin(final,candidate)
                    if valid_http(absolute):variants.append((absolute,w,h))
                    break
        if not variants and '#EXTINF:' in text and '#EXT-X-' in text and MIN_HEIGHT==0:
            variants.append((final,0,0))
        return variants
    except Exception:return []

def build_live(records,labels):
    ids={name:str(stable_id('live-category|'+name)) for name in labels}
    categories=[{'category_id':ids[name],'category_name':name,'parent_id':0} for name in labels]
    streams=[];urls={}
    for category,name,logo,url,w,h,source,epg in sorted(records.values(),key=lambda x:(x[0].casefold(),x[1].casefold(),-x[5])):
        sid=stable_id('public-live|'+category+'|'+url)
        while sid in urls and urls[sid]!=url:sid=(sid%0x7fffffff)+1
        quality='4K' if h>=2160 else '1440p' if h>=1440 else '1080p' if h>=1080 else '720p' if h>=720 else ''
        streams.append({'num':len(streams)+1,'name':name+(f' [{quality}]' if quality else ''),
            'stream_type':'live','stream_id':sid,'stream_icon':logo,'epg_channel_id':epg,
            'added':'0','category_id':ids[category],'custom_sid':'','tv_archive':0,
            'direct_source':'','tv_archive_duration':0,'container_extension':'m3u8'})
        urls[sid]=url
    return streams,urls,categories

def scan_live():
    with LOCK:
        if STATE['checking']:return
        STATE.update(checking=True,started_at=time.time(),checked=0,total=0,error=None,stats={})
    stats={};byurl={};records={};checked=0;total=0
    try:
        sources=dict(SOURCES)
        if ADULT_ENABLED:
            for i,url in enumerate(url_list(ADULT_PLAYLIST_URL+'\n'+ADULT_PLAYLIST_URLS)):
                sources['ADULT_'+str(i+1)]=url
        for i,url in enumerate(url_list(EXTRA_LIVE_URLS)):
            sources['EXTRA_'+str(i+1)]=url
        for source,url in sources.items():
            try:
                entries=parse_playlist(request(url,timeout=25,limit=32000000)[0],source)
                stats[source]={'found':len(entries),'checked':0,'accepted':0,'failed':0}
                for entry in entries:
                    if not ADULT_ENABLED and ADULT_TERMS.search(entry[1]+' '+entry[2]):continue
                    byurl.setdefault(entry[4],[]).append(entry)
            except Exception as exc:
                stats[source]={'found':0,'checked':0,'accepted':0,'failed':0,'error':str(exc)[:150]}
        unique=list(byurl)
        if MAX_CHECKS:unique=unique[:MAX_CHECKS]
        total=len(unique)
        labels=[ADULT_NAME]+sorted({category_for(e) for u in unique for e in byurl[u]}-{ADULT_NAME},key=str.casefold)
        with LOCK:STATE.update(total=total,category_labels=labels)
        if not total:
            with LOCK:STATE.update(checking=False,error='No playlist candidates; keeping last good catalog')
            return
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            iterator=iter(unique);pending={}
            def submit():
                try:u=next(iterator)
                except StopIteration:return
                pending[pool.submit(check_live,u)]=u
            for _ in range(WORKERS*2):submit()
            last=time.monotonic()
            while pending:
                finished,_=wait(pending,timeout=2,return_when=FIRST_COMPLETED)
                for future in finished:
                    url=pending.pop(future)
                    try:variants=future.result()
                    except Exception:variants=[]
                    checked+=1
                    for source in {e[0] for e in byurl[url]}:
                        stats[source]['checked']+=1
                        if not variants:stats[source]['failed']+=1
                    for entry in byurl[url]:
                        category=category_for(entry)
                        for variant,w,h in variants:
                            key=(category,entry[2],variant)
                            if key not in records:
                                records[key]=(category,entry[2],entry[3],variant,w,h,entry[0],entry[5])
                                stats[entry[0]]['accepted']+=1
                    submit()
                if checked and (checked%500==0 or time.monotonic()-last>=30):
                    with LOCK:STATE.update(checked=checked,stats={k:dict(v) for k,v in stats.items()},verified_manifest=len(records),failed=sum(v['failed'] for v in stats.values()))
                    last=time.monotonic()
        if records:
            streams,urls,categories=build_live(records,labels)
            with LOCK:
                STATE.update(streams=streams,urls=urls,categories=categories,at=time.time(),
                    checked=checked,total=total,stats=stats,verified_manifest=len(records),
                    failed=sum(v['failed'] for v in stats.values()),checking=False,error=None)
        else:
            with LOCK:STATE.update(checked=checked,total=total,stats=stats,checking=False,error='No valid HLS manifests; keeping last good catalog')
        print('OG IPTV live scan complete',checked,total,len(records),flush=True)
    except Exception as exc:
        with LOCK:STATE.update(checking=False,error=str(exc)[:250],checked=checked,total=total)
        print('Live scan error',type(exc).__name__,flush=True)

def language_code(value):
    if isinstance(value,list):value=value[0] if value else ''
    value=str(value or '').strip().lower()
    return LANGS.get(value,value[:2].upper() if re.fullmatch(r'[a-z]{2,3}',value) else 'UND')

def genre_name(value):
    if isinstance(value,list):value=value[0] if value else ''
    raw=str(value or '').strip().lower()
    for key,label in GENRES.items():
        if key in raw:return label
    return 'OTHER'

def video_extension(url):
    ext=os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower().lstrip('.')
    return ext if ext in ('mp4','mkv','webm','mov','m3u8','ts') else ''

def category_labels(item,kind):
    title=str(item.get('title') or item.get('name') or '')
    combined=' '.join(str(item.get(k) or '') for k in ('category','genre','language'))+' '+title
    if ADULT_ENABLED and (item.get('adult') is True or ADULT_TERMS.search(combined)):
        return [ADULT_MOVIE_NAME if kind=='VOD' else ADULT_SERIES_NAME]
    lang=language_code(item.get('language',''))
    genre=genre_name(item.get('genre',''))
    labels=[]
    if PERSIAN_DUB_TERMS.search(combined):labels.append('🎬 فیلم‌های دوبله فارسی' if kind=='VOD' else '📺 سریال‌های دوبله فارسی')
    elif PERSIAN_ORIGINAL_TERMS.search(combined):labels.append('🎬 فیلم‌های فارسی و ایرانی' if kind=='VOD' else '📺 سریال‌های فارسی و ایرانی')
    labels.extend([f'{kind} - {genre} [{lang}]',f'{kind} - ALL [{lang}]'])
    if item.get('recent'):labels.append(f'{kind} - RECENTLY ADDED')
    return list(dict.fromkeys(labels))

def archive_items(adult_only=False, persian_only=False, series_only=False):
    if not ARCHIVE_DISCOVERY or not ARCHIVE_MAX:return []
    query='mediatype:movies AND (licenseurl:creativecommons.org/publicdomain/zero/* OR licenseurl:creativecommons.org/publicdomain/mark/*)'
    if adult_only:query+=' AND (subject:adult OR subject:erotic OR title:adult OR title:erotic)'
    if persian_only:
        query+=' AND (language:per OR language:fas OR language:fa OR language:persian OR language:farsi OR subject:iranian OR subject:persian OR title:ایران OR title:فارسی)'
    if series_only:
        query+=' AND (subject:series OR subject:episode OR title:episode OR title:قسمت OR title:سریال)'
    params=urllib.parse.urlencode({'q':query,'fl[]':['identifier','title','language','year','subject'],
        'rows':str(ARCHIVE_MAX),'page':'1','output':'json'},doseq=True)
    docs=get_json('https://archive.org/advancedsearch.php?'+params,20).get('response',{}).get('docs',[])
    def fetch_doc(doc):
        ident=doc.get('identifier','')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}',ident):return None
        meta=get_json('https://archive.org/metadata/'+urllib.parse.quote(ident),12)
        md=meta.get('metadata',{});lic=str(md.get('licenseurl','')).lower()
        if 'creativecommons.org/publicdomain/' not in lic:return None
        videos=[f for f in meta.get('files',[]) if video_extension(f.get('name','')) in ('mp4','webm','mkv') and not f.get('private')]
        if not videos:return None
        videos.sort(key=lambda f:(f.get('source')!='original',-int(f.get('size','0') or '0')))
        url='https://archive.org/download/'+urllib.parse.quote(ident,safe='')+'/'+urllib.parse.quote(videos[0]['name'],safe='/')
        return {'title':str(md.get('title') or doc.get('title') or ident),'url':url,
            'language':md.get('language') or doc.get('language') or '',
            'genre':md.get('subject') or doc.get('subject') or '',
            'year':str(md.get('year') or doc.get('year') or ''),
            'poster':'https://archive.org/services/img/'+urllib.parse.quote(ident),
            'description':str(md.get('description') or '')[:1000],
            'source':'Internet Archive public domain','license_url':lic,'adult':adult_only,'persian_discovered':persian_only}
    result=[]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for future in as_completed([pool.submit(fetch_doc,d) for d in docs]):
            try:
                item=future.result()
                if item:result.append(item)
            except Exception:pass
    return result

def load_catalog(url):
    if not valid_http(url):return []
    data=get_json(url,20,32000000)
    if isinstance(data,list):return data
    if isinstance(data,dict):return data.get('items',[])
    return []

def licensed_catalog_feeds():
    feeds={'movies':[],'series':[],'adult_movies':[],'adult_series':[]}
    if not valid_http(LICENSED_CATALOGS_URL):return feeds
    data=get_json(LICENSED_CATALOGS_URL,20)
    if not isinstance(data,dict):raise ValueError('Invalid licensed catalog manifest')
    for kind in feeds:
        for item in data.get(kind,[])[:40]:
            if isinstance(item,dict) and valid_http(item.get('url')):
                feeds[kind].append((str(item.get('name') or 'Licensed')[:60],item['url']))
    return feeds

def scan_vod():
    with LOCK:
        if VOD['checking']:return
        VOD.update(checking=True,error=None)
    movies=[];series=[];rejected=0;sources={}
    try:
        try:
            discovered=archive_items();movies.extend(discovered);sources['archive']=len(discovered)
            if ADULT_ENABLED and ADULT_ARCHIVE_DISCOVERY:
                adult_discovered=archive_items(adult_only=True)
                movies.extend(adult_discovered)
                sources['adult_archive_discovered']=len(adult_discovered)
        except Exception as exc:sources['archive_error']=str(exc)[:160]
        # Public-domain Persian-language and Iranian film discovery.
        # Only verified public-domain-marked archive files enter the catalog.
        try:
            persian_films=archive_items(persian_only=True)
            movies.extend(persian_films)
            sources['persian_archive_movies_discovered']=len(persian_films)
        except Exception as exc:
            sources['persian_archive_movies_error']=str(exc)[:160]
        # Episodes require a verified season/episode structure; do not
        # mislabel independent videos as complete television series.
        try:
            episode_candidates=archive_items(persian_only=True,series_only=True)
            import re as _re
            grouped={}
            for episode in episode_candidates:
                title=str(episode.get('title',''))
                match=_re.search(r'(?i)^(.*?)\\s*(?:[-–:]\\s*)?(?:s(\\d{1,2})e(\\d{1,3})|season\\s*(\\d{1,2})\\s*episode\\s*(\\d{1,3})|فصل\\s*(\\d{1,2})\\s*قسمت\\s*(\\d{1,3}))',title)
                if not match or not match.group(1).strip():continue
                nums=[x for x in match.groups()[1:] if x is not None]
                if len(nums)!=2:continue
                name=match.group(1).strip()
                key=name.casefold()
                group=grouped.setdefault(key,{'title':name,'language':'fa','genre':'Iranian Persian series','poster':episode.get('poster',''),'seasons':{}})
                season,number=map(int,nums)
                group['seasons'].setdefault(season,[]).append({'number':number,'title':title,'url':episode['url']})
            for group in grouped.values():
                if sum(map(len,group['seasons'].values()))<2:continue
                group['seasons']=[{'number':n,'episodes':sorted(eps,key=lambda x:x['number'])} for n,eps in sorted(group['seasons'].items())]
                series.append(group)
            sources['persian_archive_series_discovered']=sum(1 for g in grouped.values() if isinstance(g['seasons'],list))
        except Exception as exc:
            sources['persian_archive_series_error']=str(exc)[:160]
        try:feeds=licensed_catalog_feeds()
        except Exception as exc:
            feeds={'movies':[],'series':[],'adult_movies':[],'adult_series':[]}
            sources['manifest_error']=str(exc)[:160]
        configured=[('movies',VOD_JSON_URL,movies,False),('series',SERIES_JSON_URL,series,False)]
        for i,url in enumerate(url_list(MOVIES_JSON_URLS)):configured.append((f'movies_extra_{i+1}',url,movies,False))
        for i,url in enumerate(url_list(SERIES_JSON_URLS)):configured.append((f'series_extra_{i+1}',url,series,False))
        if ADULT_ENABLED:
            for i,url in enumerate(url_list(ADULT_MOVIES_JSON_URLS)):configured.append((f'adult_movies_{i+1}',url,movies,True))
            for i,url in enumerate(url_list(ADULT_SERIES_JSON_URLS)):configured.append((f'adult_series_{i+1}',url,series,True))
        for kind in feeds:
            if kind.startswith('adult') and not ADULT_ENABLED:continue
            for name,url in feeds[kind]:
                configured.append((kind+':'+name,url,movies if 'movies' in kind else series,kind.startswith('adult')))
        for name,url,dest,adult in configured:
            if not valid_http(url):continue
            try:
                items=load_catalog(url)
                if not isinstance(items,list):raise ValueError('Catalog items must be a list')
                accepted=0
                for item in items:
                    if not isinstance(item,dict):continue
                    row=dict(item)
                    if adult:row['adult']=True
                    if not ADULT_ENABLED and (row.get('adult') is True or ADULT_TERMS.search(str(row.get('title',''))+' '+str(row.get('genre','')))):continue
                    dest.append(row);accepted+=1
                sources[name]=accepted
            except Exception as exc:sources[name+'_error']=str(exc)[:160]
        movie_rows=[];movie_info={};movie_urls={};movie_categories={};seen=set()
        for item in movies:
            title=str(item.get('title') or item.get('name') or '').strip();url=str(item.get('url') or '').strip()
            ext=video_extension(url)
            if not title or not valid_http(url) or not ext:rejected+=1;continue
            if url in seen:continue
            seen.add(url);sid=stable_id('vod|'+url)
            while sid in movie_urls and movie_urls[sid]!=url:sid=(sid%0x7fffffff)+1
            cats=category_labels(item,'VOD')
            for label in cats:movie_categories.setdefault(label,str(stable_id('movie-category|'+label)))
            base={'num':len(movie_info)+1,'name':title,'stream_type':'movie','stream_id':sid,
                'stream_icon':str(item.get('poster') or ''),'rating':'','rating_5based':0,
                'added':str(int(time.time())),'category_id':movie_categories[cats[0]],
                'container_extension':ext,'custom_sid':'','direct_source':''}
            for cat in cats:
                row=dict(base);row['category_id']=movie_categories[cat];movie_rows.append(row)
            movie_info[sid]={'info':{'name':title,'movie_image':str(item.get('poster') or ''),
                'plot':str(item.get('description') or ''),'genre':str(item.get('genre') or ''),
                'releasedate':str(item.get('year') or ''),'duration':'','rating':''},
                'movie_data':{'stream_id':sid,'name':title,'container_extension':ext}}
            movie_urls[sid]=url
        series_rows=[];series_info={};series_urls={};series_categories={};seen=set()
        for item in series:
            title=str(item.get('title') or item.get('name') or '').strip();seasons=item.get('seasons') or []
            if not title or not isinstance(seasons,list):rejected+=1;continue
            sid=stable_id('series|'+title+'|'+str(item.get('language','')))
            if sid in seen:continue
            seen.add(sid);cats=category_labels(item,'SRS')
            for label in cats:series_categories.setdefault(label,str(stable_id('series-category|'+label)))
            info_seasons=[];episodes={}
            for season in seasons:
                if not isinstance(season,dict):continue
                try:num=int(season.get('number',1))
                except (ValueError,TypeError):continue
                ep_rows=[]
                for ep in season.get('episodes',[]):
                    if not isinstance(ep,dict):continue
                    url=str(ep.get('url') or '');ext=video_extension(url)
                    if not valid_http(url) or not ext:continue
                    try:ep_num=int(ep.get('number',len(ep_rows)+1))
                    except (ValueError,TypeError):continue
                    eid=stable_id('episode|'+url)
                    while eid in series_urls and series_urls[eid]!=url:eid=(eid%0x7fffffff)+1
                    series_urls[eid]=url
                    ep_rows.append({'id':str(eid),'episode_num':ep_num,'title':str(ep.get('title') or f'Episode {ep_num}'),
                        'container_extension':ext,'info':{'movie_image':str(ep.get('poster') or item.get('poster') or ''),
                        'plot':str(ep.get('description') or '')}})
                if ep_rows:
                    info_seasons.append({'season_number':num,'name':f'Season {num}','episode_count':len(ep_rows),
                        'cover':str(item.get('poster') or '')})
                    episodes[str(num)]=sorted(ep_rows,key=lambda e:e['episode_num'])
            if not episodes:rejected+=1;continue
            series_info[sid]={'info':{'name':title,'cover':str(item.get('poster') or ''),
                'plot':str(item.get('description') or ''),'genre':str(item.get('genre') or ''),
                'releaseDate':str(item.get('year') or '')},'seasons':info_seasons,'episodes':episodes}
            base={'num':len(series_info),'name':title,'series_id':sid,'cover':str(item.get('poster') or ''),
                'plot':str(item.get('description') or ''),'cast':'','director':'',
                'genre':str(item.get('genre') or ''),'releaseDate':str(item.get('year') or ''),
                'rating':'','category_id':series_categories[cats[0]]}
            for cat in cats:
                row=dict(base);row['category_id']=series_categories[cat];series_rows.append(row)
        mc=[{'category_id':v,'category_name':k,'parent_id':0} for k,v in movie_categories.items()]
        sc=[{'category_id':v,'category_name':k,'parent_id':0} for k,v in series_categories.items()]
        with LOCK:
            if not movie_rows and not series_rows and (VOD['movies'] or VOD['series']):
                VOD.update(checking=False,error='All independent VOD feeds empty; keeping previous catalog',sources=sources)
                return
            VOD.update(movies=movie_rows,movie_info=movie_info,movie_urls=movie_urls,movie_categories=mc,
                series=series_rows,series_info=series_info,series_urls=series_urls,series_categories=sc,
                found=len(movies)+len(series),rejected=rejected,sources=sources,at=time.time(),checking=False,error=None)
        print('OG IPTV VOD ready',len(movie_info),len(series_info),flush=True)
    except Exception as exc:
        with LOCK:VOD.update(checking=False,error=str(exc)[:250],sources=sources)
        print('VOD scan error',type(exc).__name__,flush=True)

def xtream_api(action,**kwargs):
    query={'username':XTREAM_USER,'password':XTREAM_PASSWORD,'action':action};query.update(kwargs)
    url=XTREAM_URL+'/player_api.php?'+urllib.parse.urlencode(query)
    return get_json(url,timeout=45,limit=160000000)

def balanced_selection(items,category_names,limit):
    if limit<=0:return []
    if limit>=len(items):return items
    buckets=defaultdict(deque)
    for item in items:
        if isinstance(item,dict):buckets[str(item.get('category_id','unknown'))].append(item)
    def priority(cid):
        name=str(category_names.get(cid,'')).casefold()
        if PERSIAN_VOD_TERMS.search(name) or PERSIAN_DUB_TERMS.search(name):return 0
        if re.search(r'english|anglais|\ben\b|\buk\b|\busa\b|hindi|bollywood|indian',name):return 1
        if re.search(r'french|français|\bfr\b',name):return 2
        return 3
    order=sorted(buckets,key=lambda c:(priority(c),c));result=[]
    persian=[c for c in order if priority(c)==0]
    reserve=min(limit*PERSIAN_RESERVE_PERCENT//100,sum(len(buckets[c]) for c in persian))
    while persian and len(result)<reserve:
        more=[]
        for cid in persian:
            if len(result)>=reserve:break
            if buckets[cid]:result.append(buckets[cid].popleft())
            if buckets[cid]:more.append(cid)
        persian=more
    order=[c for c in order if buckets[c]]
    while order and len(result)<limit:
        more=[]
        for cid in order:
            if len(result)>=limit:break
            if buckets[cid]:result.append(buckets[cid].popleft())
            if buckets[cid]:more.append(cid)
        order=more
    return result

def vod_group(source_name,title,kind):
    raw=str(source_name or '').strip();title=str(title or '')
    combined=raw+' '+title;norm=raw.casefold()
    if ADULT_ENABLED and ADULT_TERMS.search(combined):
        return ADULT_MOVIE_NAME if kind=='MOVIES' else ADULT_SERIES_NAME
    dubbed=bool(PERSIAN_DUB_TERMS.search(combined))
    iranian=bool(PERSIAN_ORIGINAL_TERMS.search(combined))
    indian=bool(re.search(r'(?:هند|هندی|هندي|hindi|indian|bollywood)',combined,re.I))
    genre=genre_name(raw);suffix=' | '+genre if genre!='OTHER' else ''
    if dubbed:
        if indian:return ('🎬 فیلم هندی دوبله فارسی' if kind=='MOVIES' else '📺 سریال هندی دوبله فارسی')+suffix
        return ('🎬 فیلم‌های دوبله فارسی' if kind=='MOVIES' else '📺 سریال‌های دوبله فارسی')+suffix
    if iranian:return ('🎬 فیلم‌های فارسی و ایرانی' if kind=='MOVIES' else '📺 سریال‌های فارسی و ایرانی')+suffix
    m=re.search(r'\[([A-Za-z]{2,3})\]',raw,re.I)
    lang=language_code(m.group(1)) if m else ''
    if not lang:
        for word,code in [('french','FR'),('français','FR'),('english','EN'),('anglais','EN'),
                          ('german','DE'),('deutsch','DE'),('arabic','AR'),('spanish','ES'),('turkish','TR')]:
            if word in norm:lang=code;break
    if genre!='OTHER':return kind+' | '+genre+' ['+(lang or 'UND')+']'
    return kind+' | '+(raw or 'Uncategorized')

def xtream_catalog():
    if not XTREAM_READY:return
    try:
        with LOCK:UPSTREAM_META.update(status='loading',error=None)
        live_categories=xtream_api('get_live_categories') if XTREAM_LIVE_LIMIT else []
        movie_categories=xtream_api('get_vod_categories');series_categories=xtream_api('get_series_categories')
        live_names={str(x.get('category_id')):str(x.get('category_name','LIVE')) for x in live_categories if isinstance(x,dict)}
        movie_names={str(x.get('category_id')):str(x.get('category_name','OTHER')) for x in movie_categories if isinstance(x,dict)}
        series_names={str(x.get('category_id')):str(x.get('category_name','OTHER')) for x in series_categories if isinstance(x,dict)}
        mrows=[];minfo={};murls={};mcats={};srows=[];sinfo={};surls={};scats={};lrows=[];lurls={};lcats={}
        movie_ids={};series_ids={};live_ids={}
        movies=xtream_api('get_vod_streams')
        for item in balanced_selection(movies,movie_names,XTREAM_MOVIE_LIMIT):
            try:
                original=int(item['stream_id']);title=str(item.get('name','')).strip()
                if not title:continue
                ext=str(item.get('container_extension') or 'mp4').lower()
                if ext not in ('mp4','mkv','avi','ts'):continue
                source=movie_names.get(str(item.get('category_id')),'MOVIES')
                if not ADULT_ENABLED and ADULT_TERMS.search(source+' '+title):continue
                sid=stable_id('xtream-movie|'+str(original))
                while sid in murls:sid=(sid%0x7fffffff)+1
                group=vod_group(source,title,'MOVIES')
                mcats.setdefault(group,str(stable_id('upstream-movie-category|'+group)))
                mrows.append({'num':len(mrows)+1,'name':title,'stream_type':'movie','stream_id':sid,
                    'stream_icon':item.get('stream_icon',''),'rating':'','rating_5based':0,
                    'added':str(item.get('added','0')),'category_id':mcats[group],
                    'container_extension':ext,'custom_sid':'','direct_source':''})
                minfo[sid]={'info':{'name':title,'movie_image':item.get('stream_icon',''),
                    'plot':str(item.get('plot') or ''),'genre':str(item.get('genre') or ''),
                    'releasedate':str(item.get('releaseDate') or ''),'rating':str(item.get('rating') or '')},
                    'movie_data':{'stream_id':sid,'name':title,'container_extension':ext}}
                movie_ids[sid]=original;murls[sid]=('movie',original,ext)
            except (KeyError,ValueError,TypeError):continue
        del movies
        series=xtream_api('get_series')
        for item in balanced_selection(series,series_names,XTREAM_SERIES_LIMIT):
            try:
                original=int(item['series_id']);title=str(item.get('name','')).strip()
                if not title:continue
                source=series_names.get(str(item.get('category_id')),'SERIES')
                if not ADULT_ENABLED and ADULT_TERMS.search(source+' '+title):continue
                sid=stable_id('xtream-series|'+str(original))
                group=vod_group(source,title,'SERIES')
                scats.setdefault(group,str(stable_id('upstream-series-category|'+group)))
                sinfo[sid]=None;series_ids[sid]=original
                srows.append({'num':len(srows)+1,'name':title,'series_id':sid,
                    'cover':item.get('cover',''),'plot':item.get('plot',''),
                    'genre':item.get('genre',''),'releaseDate':item.get('releaseDate',''),
                    'rating':item.get('rating',''),'category_id':scats[group]})
            except (KeyError,ValueError,TypeError):continue
        del series
        live=xtream_api('get_live_streams') if XTREAM_LIVE_LIMIT else []
        def live_priority(item):
            name=str(item.get('name','')).casefold();group=str(live_names.get(str(item.get('category_id')),'')).casefold()
            return 0 if BEIN_TERMS.search(name+' '+group) or re.search(r'canal\s*\+|rmc\s*sport',name+' '+group) else 1
        selected_live=sorted(live,key=live_priority)[:XTREAM_LIVE_LIMIT]
        for item in selected_live:
            try:
                original=int(item['stream_id']);title=str(item.get('name','')).strip()
                if not title:continue
                sid=stable_id('xtream-live|'+str(original))
                while sid in lurls:sid=(sid%0x7fffffff)+1
                source=live_names.get(str(item.get('category_id')),'LIVE')
                adult=bool(ADULT_TERMS.search(title+' '+source))
                if adult and not ADULT_ENABLED:continue
                group=ADULT_NAME if adult else ('⚽ beIN SPORTS | '+source if BEIN_TERMS.search(title+' '+source) else 'POWERDINO | '+flag_label(source))
                lcats.setdefault(group,str(stable_id('upstream-live-category|'+group)))
                lrows.append({'num':len(lrows)+1,'name':title,'stream_type':'live','stream_id':sid,
                    'stream_icon':item.get('stream_icon',''),
                    'epg_channel_id':str(item.get('epg_channel_id') or item.get('tvg_id') or ''),
                    'added':'0','category_id':lcats[group],'custom_sid':'','tv_archive':0,
                    'direct_source':'','tv_archive_duration':0,'container_extension':'ts'})
                lurls[sid]=('live',original,'ts');live_ids[sid]=original
            except (KeyError,ValueError,TypeError):continue
        with LOCK:
            UPSTREAM.update(movie=murls,series=surls,live=lurls)
            UPSTREAM_MOVIE_IDS.clear();UPSTREAM_MOVIE_IDS.update(movie_ids)
            UPSTREAM_SERIES_IDS.clear();UPSTREAM_SERIES_IDS.update(series_ids)
            UPSTREAM_LIVE_IDS.clear();UPSTREAM_LIVE_IDS.update(live_ids)
            UPSTREAM_ROWS.update(live=lrows,live_categories=[{'category_id':v,'category_name':k,'parent_id':0} for k,v in lcats.items()],
                movies=mrows,movie_info=minfo,movie_categories=[{'category_id':v,'category_name':k,'parent_id':0} for k,v in mcats.items()],
                series=srows,series_info=sinfo,series_categories=[{'category_id':v,'category_name':k,'parent_id':0} for k,v in scats.items()])
            UPSTREAM_META.update(status='ready',movies=len(murls),series=len(sinfo),live=len(lurls),error=None)
        print('OG IPTV Xtream ready',len(murls),len(sinfo),len(lurls),flush=True)
    except Exception as exc:
        with LOCK:UPSTREAM_META.update(status='error',error=type(exc).__name__)
        print('Xtream catalog error',type(exc).__name__,flush=True)

def safe_text(value):return '' if isinstance(value,(dict,list)) else str(value or '')

def movie_details(sid):
    with LOCK:
        base=UPSTREAM_ROWS['movie_info'].get(sid)
        upstream_id=UPSTREAM_MOVIE_IDS.get(sid)
        if not base or upstream_id is None:return base or {}
        if base.get('_loaded'):return {k:v for k,v in base.items() if k!='_loaded'}
    try:
        with DETAIL_SEMAPHORE:details=xtream_api('get_vod_info',vod_id=upstream_id)
        if not isinstance(details,dict):return base
        info=details.get('info') if isinstance(details.get('info'),dict) else {}
        movie_data=details.get('movie_data') if isinstance(details.get('movie_data'),dict) else {}
        result_info={**base['info']}
        for key,aliases in {'name':['name'],'movie_image':['movie_image','cover_big','cover'],
            'plot':['plot','description'],'genre':['genre'],'releasedate':['releasedate','releaseDate','date'],
            'rating':['rating'],'duration':['duration'],'director':['director'],'cast':['cast','actors'],
            'youtube_trailer':['youtube_trailer']}.items():
            for alias in aliases:
                value=info.get(alias)
                if value is not None and not isinstance(value,(list,dict)) and str(value).strip():
                    result_info[key]=str(value);break
        result={'info':result_info,'movie_data':{**base['movie_data'],**{k:v for k,v in movie_data.items() if k not in ('stream_id','name','container_extension')}}}
        with LOCK:UPSTREAM_ROWS['movie_info'][sid]={**result,'_loaded':True}
        return result
    except Exception as exc:
        print('Movie detail error',type(exc).__name__,flush=True);return base

def series_details(sid):
    with LOCK:
        cached=UPSTREAM_ROWS['series_info'].get(sid)
        if isinstance(cached,dict):return cached
        upstream_id=UPSTREAM_SERIES_IDS.get(sid)
        row=next((r for r in UPSTREAM_ROWS['series'] if r['series_id']==sid),{})
    if upstream_id is None:return {}
    try:
        with DETAIL_SEMAPHORE:details=xtream_api('get_series_info',series_id=upstream_id)
        if not isinstance(details,dict):return {}
        info=details.get('info') if isinstance(details.get('info'),dict) else {}
        seasons_raw=details.get('seasons') if isinstance(details.get('seasons'),list) else []
        season_map={}
        for item in seasons_raw:
            if isinstance(item,dict):
                try:season_map[int(item.get('season_number'))]=item
                except (ValueError,TypeError):pass
        episodes={};seasons=[];new_urls={}
        for season,eps in (details.get('episodes') or {}).items():
            if not isinstance(eps,list):continue
            try:n=int(season)
            except (ValueError,TypeError):continue
            rows=[]
            for ep in eps:
                if not isinstance(ep,dict):continue
                try:
                    original=int(ep['id']);ext=str(ep.get('container_extension') or 'mp4').lower()
                    if ext not in ('mp4','mkv','avi','ts'):continue
                    eid=stable_id('xtream-episode|'+str(original))
                    meta=ep.get('info') if isinstance(ep.get('info'),dict) else {}
                    new_urls[eid]=('series',original,ext)
                    rows.append({'id':str(eid),'episode_num':int(ep.get('episode_num') or len(rows)+1),
                        'title':safe_text(ep.get('title') or 'Episode'),'container_extension':ext,
                        'added':safe_text(ep.get('added')),'season':n,
                        'info':{'movie_image':safe_text(meta.get('movie_image') or meta.get('cover') or row.get('cover')),
                        'plot':safe_text(meta.get('plot') or meta.get('description')),
                        'duration':safe_text(meta.get('duration'))}})
                except (ValueError,KeyError,TypeError):continue
            if rows:
                rows.sort(key=lambda x:x['episode_num']);episodes[str(n)]=rows
                existing=season_map.get(n,{})
                seasons.append({'season_number':n,'name':safe_text(existing.get('name') or 'Season '+str(n)),
                    'episode_count':len(rows),'cover':safe_text(existing.get('cover') or row.get('cover'))})
        seasons.sort(key=lambda x:x['season_number'])
        result={'info':{'name':safe_text(info.get('name') or row.get('name')),
            'cover':safe_text(info.get('cover') or row.get('cover')),
            'plot':safe_text(info.get('plot') or row.get('plot')),
            'genre':safe_text(info.get('genre') or row.get('genre')),
            'releaseDate':safe_text(info.get('releaseDate') or row.get('releaseDate')),
            'rating':safe_text(info.get('rating') or row.get('rating'))},
            'seasons':seasons,'episodes':episodes}
        with LOCK:
            UPSTREAM['series'].update(new_urls)
            UPSTREAM_ROWS['series_info'][sid]=result
        return result
    except Exception as exc:
        print('Series detail error',type(exc).__name__,flush=True);return {}

def xmltv_parse_date(value):
    try:
        date=datetime.datetime.strptime(value[:14],'%Y%m%d%H%M%S')
        tail=value[14:].strip()
        if re.match(r'^[+-]\d{4}',tail):
            sign=1 if tail[0]=='+' else -1
            offset=sign*(int(tail[1:3])*60+int(tail[3:5]))
            zone=datetime.timezone(datetime.timedelta(minutes=offset))
        else:zone=datetime.timezone.utc
        return int(date.replace(tzinfo=zone).timestamp())
    except (ValueError,TypeError,IndexError):return 0

def xmltv_data():
    if not valid_http(XMLTV_URL):return {}
    with XMLTV_LOCK:
        if XMLTV_STATE['at'] and time.time()-XMLTV_STATE['at']<3600:return XMLTV_STATE['channels']
        try:
            req=urllib.request.Request(XMLTV_URL,headers={'User-Agent':'OG-IPTV/'+VERSION,'Accept-Encoding':'gzip'})
            with urllib.request.urlopen(req,timeout=25) as resp:
                raw=resp.read(30000001)
                if len(raw)>30000000:raise ValueError('XMLTV feed too large')
                if resp.headers.get('Content-Encoding')=='gzip' or XMLTV_URL.endswith('.gz'):
                    raw=gzip.decompress(raw)
                    if len(raw)>120000000:raise ValueError('XMLTV decompressed feed too large')
            root=ET.fromstring(raw);guide={};now=int(time.time())
            for elem in root.iter('programme'):
                start=xmltv_parse_date(elem.get('start',''));end=xmltv_parse_date(elem.get('stop',''))
                if not start or end<now-3600 or start>now+86400:continue
                cid=elem.get('channel','');title=elem.findtext('title') or '';desc=elem.findtext('desc') or ''
                guide.setdefault(cid,[]).append({'id':cid,'title':title,'description':desc,
                    'start':str(start),'end':str(end),'start_timestamp':start,'stop_timestamp':end,
                    'now_playing':int(start<=now<end),'has_archive':0})
            XMLTV_STATE.update(at=time.time(),channels=guide,error=None)
        except Exception as exc:XMLTV_STATE.update(at=time.time(),error=type(exc).__name__)
        return XMLTV_STATE['channels']

def epg_from_xmltv(sid):
    with LOCK:
        row=next((r for r in UPSTREAM_ROWS['live'] if r['stream_id']==sid),None)
        if not row:row=next((r for r in STATE['streams'] if r['stream_id']==sid),None)
    cid=(row or {}).get('epg_channel_id','')
    entries=xmltv_data().get(cid,[]) if cid else []
    return {'epg_listings':[dict(e,title=base64.b64encode(e['title'].encode()).decode(),
        description=base64.b64encode(e['description'].encode()).decode()) for e in entries]}

def epg_from_upstream(sid,action,limit='20'):
    with LOCK:
        upstream_id=UPSTREAM_LIVE_IDS.get(sid);cached=EPG_CACHE.get((sid,action))
    if upstream_id is None:return epg_from_xmltv(sid)
    if cached and time.time()-cached[0]<EPG_CACHE_TTL:return cached[1]
    try:
        kwargs={'stream_id':upstream_id}
        if action=='get_short_epg':kwargs['limit']=str(max(1,min(int(limit),50)))
        result=xtream_api(action,**kwargs)
        if not isinstance(result,dict):result={'epg_listings':[]}
        if not result.get('epg_listings') and XMLTV_URL:result=epg_from_xmltv(sid)
        with LOCK:EPG_CACHE[(sid,action)]=(time.time(),result)
        return result
    except Exception:return epg_from_xmltv(sid)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):
        print('OG IPTV HTTP',self.command,urllib.parse.urlsplit(self.path).path,flush=True)
    def reply(self,obj,status=200):
        with tempfile.SpooledTemporaryFile(max_size=1024*1024,mode='w+b') as out:
            encoder=json.JSONEncoder(ensure_ascii=False,separators=(',',':'))
            for piece in encoder.iterencode(obj):out.write(piece.encode('utf-8'))
            size=out.tell();out.seek(0)
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(size));self.end_headers()
            if self.command!='HEAD':
                try:shutil.copyfileobj(out,self.wfile,1024*128)
                except (BrokenPipeError,ConnectionResetError):pass
    def reply_catalog(self,groups,category_id=''):
        with tempfile.SpooledTemporaryFile(max_size=1024*1024,mode='w+b') as out:
            encoder=json.JSONEncoder(ensure_ascii=False,separators=(',',':'));out.write(b'[');first=True
            for group in groups:
                for item in group:
                    if category_id and str(item.get('category_id'))!=category_id:continue
                    if not first:out.write(b',')
                    first=False
                    for piece in encoder.iterencode(item):out.write(piece.encode('utf-8'))
            out.write(b']');size=out.tell();out.seek(0)
            self.send_response(200);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(size));self.end_headers()
            if self.command!='HEAD':
                try:shutil.copyfileobj(out,self.wfile,1024*128)
                except (BrokenPipeError,ConnectionResetError):pass
    def proxy_upstream(self,kind,upstream_id,ext):
        if not XTREAM_READY:return self.reply({'error':'Upstream disabled'},503)
        url=XTREAM_URL+'/'+kind+'/'+urllib.parse.quote(XTREAM_USER,safe='')+'/'+urllib.parse.quote(XTREAM_PASSWORD,safe='')+'/'+str(upstream_id)+'.'+ext
        headers={'User-Agent':'OG-IPTV/'+VERSION,'Accept-Encoding':'identity'}
        for key in ('Range','If-Range'):
            if self.headers.get(key):headers[key]=self.headers[key]
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=25) as upstream:
                self.send_response(upstream.status)
                for key in ('Content-Type','Content-Length','Content-Range','Accept-Ranges','ETag','Last-Modified'):
                    value=upstream.headers.get(key)
                    if value:self.send_header(key,value)
                self.send_header('Cache-Control','no-store');self.end_headers()
                if self.command!='HEAD':
                    while True:
                        chunk=upstream.read(65536)
                        if not chunk:break
                        self.wfile.write(chunk)
        except (BrokenPipeError,ConnectionResetError):pass
        except urllib.error.HTTPError as exc:
            if exc.code==416:
                self.send_response(416)
                if exc.headers.get('Content-Range'):self.send_header('Content-Range',exc.headers['Content-Range'])
                self.send_header('Content-Length','0');self.end_headers();return
            self.reply({'error':'Upstream HTTP '+str(exc.code)},502)
        except Exception:self.reply({'error':'Upstream unavailable'},502)
    def do_HEAD(self):self.reply({'ok':True})
    def do_GET(self):
        parsed=urllib.parse.urlsplit(self.path);path=parsed.path
        params=urllib.parse.parse_qs(parsed.query);get=lambda k:params.get(k,[''])[0]
        api_path=path in ('/player_api.php','/get.php')
        if api_path and (get('username')!=USER or get('password')!=PASSWORD):
            return self.reply({'user_info':{'auth':0,'status':'Disabled'}})
        def login():
            public=urllib.parse.urlsplit(PUBLIC_URL);secure=public.scheme=='https'
            return {'user_info':{'username':USER,'password':PASSWORD,'auth':1,'status':'Active',
                'exp_date':None,'is_trial':'0','active_cons':'0','max_connections':'1',
                'allowed_output_formats':['m3u8','mp4','mkv']},
                'server_info':{'url':public.hostname,'port':str(public.port or (443 if secure else 80)),
                'https_port':'443' if secure else '','server_protocol':public.scheme,
                'timezone':'Europe/Zurich','timestamp_now':int(time.time())}}
        if path=='/player_api.php' and not get('action'):return self.reply(login())
        with LOCK:
            live=dict(STATE);vod=dict(VOD);extra=dict(UPSTREAM_ROWS);upstream_meta=dict(UPSTREAM_META)
        action=get('action') if api_path else ''
        need_live=(path in ('/','/health','/get.php','/diagnostics') or path.startswith(('/live/','/movie/','/series/')) or action in ('get_live_categories','get_live_streams'))
        need_vod=(path in ('/','/health','/diagnostics') or path.startswith(('/live/','/movie/','/series/')) or action in ('get_vod_categories','get_vod_streams','get_vod_info','get_series_categories','get_series','get_series_info'))
        if need_live:
            if action!='get_live_streams':live['streams']=live['streams']+extra['live']
            live['categories']=live['categories']+extra['live_categories']
        if need_vod:
            if action!='get_vod_streams':vod['movies']=vod['movies']+extra['movies']
            vod['movie_categories']=vod['movie_categories']+extra['movie_categories']
            if action!='get_series':vod['series']=vod['series']+extra['series']
            vod['series_categories']=vod['series_categories']+extra['series_categories']
            if action=='get_vod_info':vod['movie_info']={**vod['movie_info'],**extra['movie_info']}
            if action=='get_series_info':vod['series_info']={**vod['series_info'],**extra['series_info']}
        if path in ('/','/health'):
            category_lookup={str(c['category_id']):str(c['category_name']) for c in live['categories']}
            movie_names={str(c.get('category_id')):str(c.get('category_name','')) for c in vod['movie_categories']}
            series_names={str(c.get('category_id')):str(c.get('category_name','')) for c in vod['series_categories']}
            def count_category(rows,names,pattern,key):
                return len({str(r.get(key,'')) for r in rows if pattern.search(names.get(str(r.get('category_id')),''))})
            adult_movies=count_category(vod['movies'],movie_names,re.compile('بزرگسالان'), 'stream_id')
            adult_series=count_category(vod['series'],series_names,re.compile('بزرگسالان'), 'series_id')
            elapsed=max(0,time.time()-live['started_at']) if live['checking'] else 0
            remaining=max(0,live['total']-live['checked'])
            eta=round(remaining*elapsed/live['checked']) if live['checking'] and live['checked']>0 else None
            return self.reply({'ok':True,'name':'OG IPTV','version':VERSION,
                'streams_loaded':len(live['streams']),'last_scan':int(live['at']),
                'scanning':live['checking'],'scan_error':live['error'],
                'scan_checked':live['checked'],'scan_total':live['total'],'scan_remaining':remaining,
                'scan_progress_percent':round(100*live['checked']/live['total'],2) if live['total'] else 0,
                'scan_eta_seconds':eta,'scan_elapsed_seconds':round(elapsed),
                'scan_manifest_candidates_accepted':live['verified_manifest'],
                'scan_playback_verified':live['verified_playback'],
                'scan_failed_candidates':live['failed'],
                'verification_note':'HLS manifest reachable; actual video/audio playback is not verified',
                'categories_loaded':len(live['categories']),
                'adult_streams_loaded':sum(bool(ADULT_TERMS.search(str(s.get('name',''))+' '+category_lookup.get(str(s['category_id']),''))) for s in live['streams']),
                'bein_streams_loaded':sum(bool(BEIN_TERMS.search(str(s.get('name','')))) for s in live['streams']),
                'adult_playlist_configured':bool(ADULT_PLAYLIST_URL or ADULT_PLAYLIST_URLS),
                'adult_live_discovery_note':'Operator-supplied authorized playlists only',
                'minimum_verified_height':MIN_HEIGHT,
                'vod_scanning':vod['checking'],'vod_error':vod['error'],
                'movies_loaded':len(vod['movie_info'])+len(extra['movie_info']),
                'series_loaded':len(vod['series_info'])+len(extra['series_info']),
                'movie_categories_loaded':len(vod['movie_categories']),
                'adult_movies_loaded':adult_movies,
                'adult_series_loaded':adult_series,
                'adult_live_loaded':sum(bool(ADULT_TERMS.search(str(s.get('name',''))+' '+category_lookup.get(str(s['category_id']),''))) for s in live['streams']),
                'persian_movie_categories':sum(bool(PERSIAN_VOD_TERMS.search(n)) for n in movie_names.values()),
                'persian_series_categories':sum(bool(PERSIAN_VOD_TERMS.search(n)) for n in series_names.values()),
                'persian_dubbed_movie_categories':sum(bool(PERSIAN_DUB_TERMS.search(n)) for n in movie_names.values()),
                'persian_dubbed_series_categories':sum(bool(PERSIAN_DUB_TERMS.search(n)) for n in series_names.values()),
                'persian_dubbed_movies_loaded':count_category(vod['movies'],movie_names,PERSIAN_DUB_TERMS,'stream_id'),
                'persian_dubbed_series_loaded':count_category(vod['series'],series_names,PERSIAN_DUB_TERMS,'series_id'),
                'persian_original_movies_loaded':count_category(vod['movies'],movie_names,re.compile('فارسی و ایرانی'),'stream_id'),
                'persian_original_series_loaded':count_category(vod['series'],series_names,re.compile('فارسی و ایرانی'),'series_id'),
                'persian_reserve_percent':PERSIAN_RESERVE_PERCENT,
                'series_categories_loaded':len(vod['series_categories']),'vod_last_scan':int(vod['at']),
                'xtream_config':{'enabled':XTREAM_ENABLED,'url_present':bool(XTREAM_URL),
                    'user_present':bool(XTREAM_USER),'password_present':bool(XTREAM_PASSWORD),'ready':bool(XTREAM_READY)},
                'xtream':upstream_meta,
                'independent_sources':{'manifest_configured':bool(LICENSED_CATALOGS_URL),
                    'movies_feed_configured':bool(VOD_JSON_URL or MOVIES_JSON_URLS),
                    'series_feed_configured':bool(SERIES_JSON_URL or SERIES_JSON_URLS),
                    'adult_movies_feed_configured':bool(ADULT_MOVIES_JSON_URLS),
                    'adult_series_feed_configured':bool(ADULT_SERIES_JSON_URLS),
                    'adult_live_feed_count':len(url_list(ADULT_PLAYLIST_URL+'\n'+ADULT_PLAYLIST_URLS)),
                    'extra_live_configured':bool(EXTRA_LIVE_URLS),'vod_sources':vod['sources'],'refresh_hours':REFRESH_HOURS},
                'source_breakdown':{'live_public':len(STATE['streams']),'live_subscription':len(extra['live']),
                    'movies_subscription':len(extra['movies']),'series_subscription':len(extra['series']),
                    'bein_subscription':sum(bool(BEIN_TERMS.search(str(r.get('name','')))) for r in extra['live']),
                    'adult_subscription':sum(bool(ADULT_TERMS.search(str(r.get('name','')))) for r in extra['live'])},
                'catalog_limits':{'live':XTREAM_LIVE_LIMIT,'movies':XTREAM_MOVIE_LIMIT,'series':XTREAM_SERIES_LIMIT},
                'epg':{'upstream_on_demand':bool(XTREAM_READY),'xmltv_configured':bool(XMLTV_URL),
                    'xmltv_channels':len(XMLTV_STATE['channels']),'xmltv_error':XMLTV_STATE['error'],
                    'epg_channel_ids':sum(bool(s.get('epg_channel_id')) for s in live['streams'])}})
        if path=='/diagnostics':
            if get('username')!=USER or get('password')!=PASSWORD:return self.reply({'error':'Unauthorized'},401)
            return self.reply({'version':VERSION,'stats':live['stats'],'checked':live['checked'],
                'total':live['total'],'accepted_total':len(STATE['streams']),
                'scanning':live['checking'],'vod_sources':vod['sources'],
                'vod_rejected':vod['rejected'],'vod_error':vod['error']})
        parts=path.strip('/').split('/')
        if len(parts)==3 or (len(parts)==4 and parts[0] in ('live','movie','series')):
            try:
                if len(parts)==3:kind='live';u,pw,filename=parts
                else:kind,u,pw,filename=parts
                if urllib.parse.unquote(u)!=USER or urllib.parse.unquote(pw)!=PASSWORD:
                    return self.reply({'error':'Unauthorized'},401)
                sid=int(filename.split('.')[0])
            except (ValueError,IndexError):return self.reply({'error':'Invalid stream'},400)
            with LOCK:
                upstream=UPSTREAM.get(kind,{}).get(sid)
                target=(STATE['urls'] if kind=='live' else VOD['movie_urls'] if kind=='movie' else VOD['series_urls']).get(sid)
            if upstream:return self.proxy_upstream(*upstream)
            if not target:return self.reply({'error':'Unknown stream'},404)
            self.send_response(302);self.send_header('Location',target)
            self.send_header('Cache-Control','no-store');self.end_headers();return
        if path=='/xmltv.php':
            if get('username')!=USER or get('password')!=PASSWORD:return self.reply({'error':'Unauthorized'},401)
            if not valid_http(XMLTV_URL):return self.reply({'error':'XMLTV_URL not configured'},404)
            try:
                with urllib.request.urlopen(urllib.request.Request(XMLTV_URL,headers={'User-Agent':'OG-IPTV/'+VERSION}),timeout=30) as resp:
                    raw=resp.read(30000001)
                    if len(raw)>30000000:return self.reply({'error':'Guide too large'},413)
                    content_type=resp.headers.get('Content-Type','application/xml')
                self.send_response(200);self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            except Exception:return self.reply({'error':'Guide unavailable'},502)
            return
        if path not in ('/player_api.php','/get.php'):return self.reply({'error':'Not found'},404)
        if path=='/get.php':
            lines=['#EXTM3U'];cats={str(c['category_id']):c['category_name'] for c in live['categories']}
            for s in live['streams']:
                group=cats.get(str(s['category_id']),'OTHER')
                target=(PUBLIC_URL+'/live/'+urllib.parse.quote(USER,safe='')+'/'+
                        urllib.parse.quote(PASSWORD,safe='')+'/'+str(s['stream_id'])+'.m3u8')
                lines.extend([f'#EXTINF:-1 tvg-logo="{s["stream_icon"]}" group-title="{group}",{s["name"]}',target])
            body='\n'.join(lines).encode('utf-8')
            self.send_response(200);self.send_header('Content-Type','application/x-mpegURL')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
        cid=get('category_id')
        if action=='get_live_categories':return self.reply(live['categories'])
        if action=='get_live_streams':return self.reply_catalog((STATE['streams'],extra['live']),cid)
        if action=='get_vod_categories':return self.reply(vod['movie_categories'])
        if action=='get_vod_streams':return self.reply_catalog((VOD['movies'],extra['movies']),cid)
        if action=='get_vod_info':
            try:sid=int(get('vod_id'))
            except ValueError:return self.reply({})
            return self.reply(movie_details(sid) if sid in UPSTREAM_MOVIE_IDS else (vod['movie_info'].get(sid) or {}))
        if action=='get_series_categories':return self.reply(vod['series_categories'])
        if action=='get_series':return self.reply_catalog((VOD['series'],extra['series']),cid)
        if action=='get_series_info':
            try:sid=int(get('series_id'))
            except ValueError:return self.reply({})
            return self.reply(series_details(sid) if sid in UPSTREAM_SERIES_IDS else (vod['series_info'].get(sid) or {}))
        if action in ('get_short_epg','get_simple_data_table'):
            try:sid=int(get('stream_id'))
            except (ValueError,TypeError):return self.reply({'epg_listings':[]})
            return self.reply(epg_from_upstream(sid,action,get('limit') or '20'))
        return self.reply(login())

def refresh_loop(fn,interval):
    while True:
        try:fn()
        except Exception as exc:print('Refresh error',fn.__name__,type(exc).__name__,flush=True)
        time.sleep(interval)

if __name__=='__main__':
    Thread(target=refresh_loop,args=(scan_vod,REFRESH_HOURS*3600),daemon=True).start()
    if XTREAM_READY:Thread(target=refresh_loop,args=(xtream_catalog,REFRESH_HOURS*3600),daemon=True).start()
    Thread(target=refresh_loop,args=(scan_live,REFRESH_HOURS*3600),daemon=True).start()
    print('OG IPTV v'+VERSION+' running on',HOST,PORT,flush=True)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
