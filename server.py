#!/usr/bin/env python3
"""OG IPTV 3.7: incremental public live TV + licensed VOD/series Xtream API.

VOD_JSON_URL / SERIES_JSON_URL point to JSON catalogs of content you can stream.
Optional Internet Archive discovery is limited to explicitly public-domain/CC0 items.
No premium subscription circumvention or copyrighted catalog scraping.
"""
import os, re, json, time, hashlib, urllib.request, urllib.parse, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
from threading import RLock, Thread

HOST=os.getenv('HOST','0.0.0.0'); PORT=int(os.getenv('PORT','8080'))
USER=os.getenv('IPTV_USER','ogdemo'); PASSWORD=os.getenv('IPTV_PASSWORD','change-me-now')
PUBLIC_URL=os.getenv('PUBLIC_URL','https://og-iptv.onrender.com').rstrip('/')
CACHE_SECONDS=max(600,int(os.getenv('CACHE_SECONDS','21600')))
WORKERS=max(1,min(int(os.getenv('CHECK_WORKERS','12')),24))
MIN_HEIGHT=max(1080,int(os.getenv('MIN_HEIGHT','1080')))
MAX_CHECKS=max(0,int(os.getenv('MAX_CHECKS','0')))
ADULT_PLAYLIST_URL=os.getenv('ADULT_PLAYLIST_URL','').strip()
VOD_JSON_URL=os.getenv('VOD_JSON_URL','').strip()
SERIES_JSON_URL=os.getenv('SERIES_JSON_URL','').strip()
ARCHIVE_DISCOVERY=os.getenv('ARCHIVE_DISCOVERY','1')=='1'
ARCHIVE_MAX=max(0,min(int(os.getenv('ARCHIVE_MAX','60')),200))
# Optional authorized Xtream connection. Never put these credentials in GitHub.
XTREAM_URL=os.getenv('XTREAM_URL','').rstrip('/')
XTREAM_USER=os.getenv('XTREAM_USER','')
XTREAM_PASSWORD=os.getenv('XTREAM_PASSWORD','')
XTREAM_ENABLED=os.getenv('XTREAM_ENABLED','0')=='1'
XTREAM_MOVIE_LIMIT=max(0,min(int(os.getenv('XTREAM_MOVIE_LIMIT','500')),5000))
XTREAM_SERIES_LIMIT=max(0,min(int(os.getenv('XTREAM_SERIES_LIMIT','30')),1000))
XTREAM_LIVE_LIMIT=max(0,min(int(os.getenv('XTREAM_LIVE_LIMIT','0')),1000))
XTREAM_READY=XTREAM_ENABLED and all((XTREAM_URL,XTREAM_USER,XTREAM_PASSWORD))
UPSTREAM={'live':{},'movie':{},'series':{}}
UPSTREAM_META={'status':'disabled','movies':0,'series':0,'live':0,'error':None}

BASE='https://iptv-org.github.io/iptv/'
SOURCES={'COUNTRIES':BASE+'index.country.m3u','CATEGORIES':BASE+'index.category.m3u'}
ADULT_NAME='🔞 ADULT 18+'
LOCK=RLock()
STATE={'at':0,'started_at':0,'checking':False,'error':None,'total':0,'checked':0,
       'streams':[],'urls':{},'categories':[{'category_id':'1','category_name':ADULT_NAME,'parent_id':0}],
       'stats':{},'category_labels':[ADULT_NAME]}
VOD={'checking':False,'at':0,'error':None,'movies':[],'movie_info':{},'movie_urls':{},
     'movie_categories':[],'series':[],'series_info':{},'series_urls':{},'series_categories':[],
     'found':0,'rejected':0,'sources':{}}

def valid_http(url):
    try:
        p=urllib.parse.urlsplit(str(url))
        return p.scheme in ('http','https') and bool(p.hostname) and not p.username and not p.password
    except Exception:return False

def request(url,timeout=9,limit=16000000):
    if not valid_http(url):raise ValueError('Invalid URL')
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 OG-IPTV/3.7','Accept':'*/*'})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return r.read(limit),r.headers.get('Content-Type',''),r.geturl()

def get_json(url,timeout=12):
    return json.loads(request(url,timeout=timeout,limit=12000000)[0].decode('utf-8'))

def parse_playlist(raw,source):
    result=[];info=''
    for line in raw.decode('utf-8','replace').splitlines():
        line=line.strip()
        if line.startswith('#EXTINF:'):info=line
        elif info and line.startswith(('https://','http://')):
            group=re.search(r'group-title="([^"]*)"',info)
            logo=re.search(r'tvg-logo="([^"]*)"',info)
            result.append((source,(group.group(1).strip() if group else '') or 'OTHER',
                           info.rsplit(',',1)[-1].strip() or 'Unnamed',logo.group(1) if logo else '',line))
            info=''
    return result

def check_live(url):
    try:
        raw,_,final=request(url,timeout=8,limit=262144)
        text=raw.decode('utf-8','replace').lstrip('\ufeff \r\n')
        if not text.startswith('#EXTM3U'):return []
        lines=text.splitlines();variants=[]
        for i,line in enumerate(lines):
            if not line.startswith('#EXT-X-STREAM-INF:'):continue
            m=re.search(r'RESOLUTION=(\d+)x(\d+)',line,re.I)
            if not m:continue
            w,h=int(m.group(1)),int(m.group(2))
            if h<MIN_HEIGHT:continue
            for candidate in lines[i+1:]:
                candidate=candidate.strip()
                if candidate and not candidate.startswith('#'):
                    absolute=urllib.parse.urljoin(final,candidate)
                    if valid_http(absolute):variants.append((absolute,w,h))
                    break
        return variants
    except Exception:return []

def category_for(entry):
    if entry[0]=='ADULT':return ADULT_NAME
    return ('🌍 ' if entry[0]=='COUNTRIES' else '📺 ')+entry[1]

def stable_id(seed):
    return (int(hashlib.sha256(seed.encode()).hexdigest()[:8],16)&0x7fffffff) or 1

def sort_name(name):return re.sub(r'\s+(?:4k|uhd|fhd|hd|1080p|1440p)$','',name,flags=re.I).casefold()

def publish_live(records,stats,checked,total,done=False,error=None):
    with LOCK:labels=list(STATE['category_labels'])
    ids={label:str(i+1) for i,label in enumerate(labels)}
    categories=[{'category_id':ids[x],'category_name':x,'parent_id':0} for x in labels]
    ordered=sorted(records.values(),key=lambda x:(x[0].casefold(),sort_name(x[1]),-x[5],x[1].casefold()))
    streams=[];urls={}
    for category,name,logo,url,w,h,source in ordered:
        sid=stable_id(category+'|'+url)
        while sid in urls and urls[sid]!=url:sid=(sid%0x7fffffff)+1
        quality='4K' if h>=2160 else ('1440p' if h>=1440 else '1080p')
        streams.append({'num':len(streams)+1,'name':f'{name} [{quality}]','stream_type':'live',
                        'stream_id':sid,'stream_icon':logo,'epg_channel_id':'','added':'0',
                        'category_id':ids[category],'custom_sid':'','tv_archive':0,
                        'direct_source':'','tv_archive_duration':0,'container_extension':'m3u8'})
        urls[sid]=url
    with LOCK:
        STATE.update(streams=streams,urls=urls,categories=categories,stats={k:dict(v) for k,v in stats.items()},
                     checked=checked,total=total,error=error)
        if done:STATE.update(at=time.time(),checking=False)

def scan_live():
    with LOCK:
        if STATE['checking']:return
        STATE.update(checking=True,started_at=time.time(),checked=0,total=0,error=None,stats={},streams=[],urls={},at=0)
    stats={};byurl={};records={};checked=0;total=0
    try:
        sources=dict(SOURCES)
        if valid_http(ADULT_PLAYLIST_URL):sources['ADULT']=ADULT_PLAYLIST_URL
        for source,url in sources.items():
            try:
                raw,_,_=request(url,timeout=25)
                entries=parse_playlist(raw,source)
                if source=='ADULT':entries=[('ADULT',ADULT_NAME,n,l,u) for _,_,n,l,u in entries]
                stats[source]={'found':len(entries),'checked':0,'accepted':0}
                for entry in entries:byurl.setdefault(entry[4],[]).append(entry)
            except Exception as e:
                stats[source]={'found':0,'checked':0,'accepted':0,'error':str(e)[:150]}
                print('Live playlist error',source,str(e)[:100],flush=True)
        unique=list(byurl)
        if MAX_CHECKS:unique=unique[:MAX_CHECKS]
        total=len(unique)
        labels=sorted({category_for(e) for members in byurl.values() for e in members}-{ADULT_NAME},key=str.casefold)
        with LOCK:STATE['category_labels']=[ADULT_NAME]+labels
        publish_live(records,stats,checked,total)
        print('OG IPTV 3.7 live candidates',total,flush=True)
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
                for f in finished:
                    url=pending.pop(f)
                    try:variants=f.result()
                    except Exception:variants=[]
                    checked+=1
                    for source in {e[0] for e in byurl[url]}:stats[source]['checked']+=1
                    for entry in byurl[url]:
                        category=category_for(entry);source,_,name,logo,_=entry
                        for variant,w,h in variants:
                            key=(category,name,variant)
                            if key not in records:
                                records[key]=(category,name,logo,variant,w,h,source)
                                stats[source]['accepted']+=1
                    submit()
                if checked and (checked%25==0 or time.monotonic()-last>=10):
                    publish_live(records,stats,checked,total)
                    print('OG IPTV 3.7 live progress',checked,'/',total,'streams',len(records),flush=True)
                    last=time.monotonic()
        publish_live(records,stats,checked,total,done=True)
        print('OG IPTV 3.7 live complete',len(records),flush=True)
    except Exception as e:
        publish_live(records,stats,checked,total,done=True,error=str(e)[:250]);print('Live scan error',repr(e),flush=True)

LANGS={'fr':'FR','fra':'FR','french':'FR','français':'FR','en':'EN','eng':'EN','english':'EN',
       'fa':'FA','fas':'FA','per':'FA','persian':'FA','farsi':'FA','فارسی':'FA',
       'de':'DE','ger':'DE','deu':'DE','german':'DE','es':'ES','spa':'ES','spanish':'ES',
       'tr':'TR','tur':'TR','turkish':'TR','it':'IT','ita':'IT','italian':'IT',
       'ar':'AR','ara':'AR','arabic':'AR','hi':'HI','hin':'HI','hindi':'HI',
       'ja':'JA','jpn':'JA','japanese':'JA','ko':'KO','kor':'KO','korean':'KO',
       'pt':'PT','por':'PT','portuguese':'PT','ru':'RU','rus':'RU','russian':'RU'}
GENRES={'action':'ACTION','comedy':'COMEDY','horror':'HORROR','drama':'DRAMA','romance':'ROMANCE',
        'thriller':'THRILLER','documentary':'DOCUMENTARY','animation':'ANIMATION',
        'science fiction':'SCI-FI','sci-fi':'SCI-FI','family':'FAMILY','adventure':'ADVENTURE',
        'mystery':'MYSTERY','fantasy':'FANTASY','music':'MUSIC','history':'HISTORY',
        'اکشن':'ACTION','کمدی':'COMEDY','ترسناک':'HORROR','درام':'DRAMA','عاشقانه':'ROMANCE'}

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
    lang=language_code(item.get('language',''))
    genre=genre_name(item.get('genre',''))
    labels=[f'{kind} - {genre} [{lang}]',f'{kind} - ALL [{lang}]']
    if item.get('recent'):labels.insert(0,f'{kind} - RECENTLY ADDED')
    return labels

def archive_items():
    """Internet Archive advanced search: only records explicitly marked public-domain/CC0."""
    if not ARCHIVE_DISCOVERY or ARCHIVE_MAX==0:return []
    query='mediatype:movies AND (licenseurl:creativecommons.org/publicdomain/zero/* OR licenseurl:creativecommons.org/publicdomain/mark/*)'
    params=urllib.parse.urlencode({'q':query,'fl[]':['identifier','title','language','year','date','subject'],
                                   'rows':str(ARCHIVE_MAX),'page':'1','output':'json'},doseq=True)
    data=get_json('https://archive.org/advancedsearch.php?'+params,timeout=20)
    docs=data.get('response',{}).get('docs',[])
    results=[]
    def fetch_doc(doc):
        ident=doc.get('identifier','')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}',ident):return None
        meta=get_json('https://archive.org/metadata/'+urllib.parse.quote(ident),timeout=12)
        md=meta.get('metadata',{})
        lic=str(md.get('licenseurl','')).lower()
        if 'creativecommons.org/publicdomain/' not in lic:return None
        files=meta.get('files',[])
        videos=[f for f in files if video_extension(f.get('name','')) in ('mp4','webm','mkv') and not f.get('private')]
        if not videos:return None
        videos.sort(key=lambda f:(f.get('source')!='original',-int(f.get('size','0') or '0')))
        filename=videos[0]['name']
        url='https://archive.org/download/'+urllib.parse.quote(ident,safe='')+'/'+urllib.parse.quote(filename,safe='/')
        return {'title':str(md.get('title') or doc.get('title') or ident),'url':url,
                'language':md.get('language') or doc.get('language') or '',
                'genre':md.get('subject') or doc.get('subject') or '',
                'year':str(md.get('year') or doc.get('year') or ''),
                'poster':'https://archive.org/services/img/'+urllib.parse.quote(ident),
                'description':str(md.get('description') or '')[:1000],
                'recent':False,'source':'Internet Archive public domain'}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for f in as_completed([pool.submit(fetch_doc,d) for d in docs]):
            try:
                item=f.result()
                if item:results.append(item)
            except Exception:pass
    return results

def load_catalog(url):
    if not valid_http(url):return []
    data=get_json(url,timeout=20)
    if isinstance(data,list):return data
    if isinstance(data,dict):return data.get('items',[])
    return []

def scan_vod():
    with LOCK:
        if VOD['checking']:return
        VOD.update(checking=True,error=None)
    movies=[];series=[];rejected=0;sources={}
    try:
        try:
            discovered=archive_items();movies.extend(discovered);sources['archive']=len(discovered)
        except Exception as e:sources['archive_error']=str(e)[:160]
        for name,url,dest in [('movies',VOD_JSON_URL,movies),('series',SERIES_JSON_URL,series)]:
            if valid_http(url):
                try:
                    items=load_catalog(url)
                    if not isinstance(items,list):raise ValueError('Catalog items must be a list')
                    dest.extend(x for x in items if isinstance(x,dict))
                    sources[name]=len(items)
                except Exception as e:sources[name+'_error']=str(e)[:160]
        movie_rows=[];movie_info={};movie_urls={};movie_categories={};seen=set()
        for item in movies:
            title=str(item.get('title') or item.get('name') or '').strip()
            url=str(item.get('url') or '').strip()
            ext=video_extension(url)
            if not title or not valid_http(url) or not ext:rejected+=1;continue
            key=(title.casefold(),url)
            if key in seen:continue
            seen.add(key)
            sid=stable_id('vod|'+url)
            while sid in movie_urls and movie_urls[sid]!=url:sid=(sid%0x7fffffff)+1
            cats=category_labels(item,'VOD')
            for label in cats:movie_categories.setdefault(label,str(len(movie_categories)+1))
            year=str(item.get('year') or '')
            movie_rows.append({'num':len(movie_rows)+1,'name':title,'stream_type':'movie',
                'stream_id':sid,'stream_icon':str(item.get('poster') or ''),'rating':'',
                'rating_5based':0,'added':str(int(time.time())),'category_id':movie_categories[cats[0]],
                'container_extension':ext,'custom_sid':'','direct_source':''})
            movie_info[sid]={'info':{'name':title,'movie_image':str(item.get('poster') or ''),
                'plot':str(item.get('description') or ''),'genre':str(item.get('genre') or ''),
                'releasedate':year,'duration':'','rating':''},
                'movie_data':{'stream_id':sid,'name':title,'container_extension':ext}}
            movie_urls[sid]=url
            # duplicate entries for additional category memberships (same stream id)
            for extra in cats[1:]:
                row=dict(movie_rows[-1]);row['category_id']=movie_categories[extra];movie_rows.append(row)
        series_rows=[];series_info={};series_urls={};series_categories={};seen=set()
        for item in series:
            title=str(item.get('title') or item.get('name') or '').strip()
            seasons=item.get('seasons') or []
            if not title or not isinstance(seasons,list):rejected+=1;continue
            sid=stable_id('series|'+title+'|'+str(item.get('language','')))
            if sid in seen:continue
            seen.add(sid)
            cats=category_labels(item,'SRS')
            for label in cats:series_categories.setdefault(label,str(len(series_categories)+1))
            info_seasons=[];episodes={}
            for season in seasons:
                if not isinstance(season,dict):continue
                try:num=int(season.get('number',1))
                except (ValueError,TypeError):continue
                ep_rows=[]
                for ep in season.get('episodes',[]):
                    if not isinstance(ep,dict):continue
                    url=str(ep.get('url') or '')
                    ext=video_extension(url)
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
            base={'num':len(seen),'name':title,'series_id':sid,'cover':str(item.get('poster') or ''),
                  'plot':str(item.get('description') or ''),'cast':'','director':'','genre':str(item.get('genre') or ''),
                  'releaseDate':str(item.get('year') or ''),'rating':'','category_id':series_categories[cats[0]]}
            series_rows.append(base)
            for extra in cats[1:]:
                row=dict(base);row['category_id']=series_categories[extra];series_rows.append(row)
        mc=[{'category_id':v,'category_name':k,'parent_id':0} for k,v in movie_categories.items()]
        sc=[{'category_id':v,'category_name':k,'parent_id':0} for k,v in series_categories.items()]
        with LOCK:
            VOD.update(movies=movie_rows,movie_info=movie_info,movie_urls=movie_urls,movie_categories=mc,
                series=series_rows,series_info=series_info,series_urls=series_urls,series_categories=sc,
                found=len(movies)+len(series),rejected=rejected,sources=sources,at=time.time(),checking=False,error=None)
        print('OG IPTV 3.7 VOD movies',len(movie_info),'series',len(series_info),'sources',sources,flush=True)
    except Exception as e:
        with LOCK:VOD.update(checking=False,error=str(e)[:250],sources=sources)
        print('OG IPTV VOD error',repr(e),flush=True)

def xtream_api(action, **kwargs):
    query={'username':XTREAM_USER,'password':XTREAM_PASSWORD,'action':action}
    query.update(kwargs)
    url=XTREAM_URL+'/player_api.php?'+urllib.parse.urlencode(query)
    raw,_,_=request(url,timeout=45,limit=80000000)
    return json.loads(raw)


def xtream_catalog():
    """Build limited, read-only upstream catalog. Does not download media."""
    if not XTREAM_READY:return
    try:
        with LOCK:UPSTREAM_META.update(status='loading',error=None)
        movie_categories=xtream_api('get_vod_categories')
        series_categories=xtream_api('get_series_categories')
        movies=xtream_api('get_vod_streams')
        series=xtream_api('get_series')
        live=xtream_api('get_live_streams') if XTREAM_LIVE_LIMIT else []
        movie_names={str(x.get('category_id')):str(x.get('category_name','OTHER')) for x in movie_categories if isinstance(x,dict)}
        series_names={str(x.get('category_id')):str(x.get('category_name','OTHER')) for x in series_categories if isinstance(x,dict)}
        # Upstream ids are namespaced to avoid collisions with independent media.
        mrows=[];minfo={};murls={};mcats={};srows=[];sinfo={};surls={};scats={};lrows=[];lurls={};lcats={}
        for item in movies[:XTREAM_MOVIE_LIMIT]:
            try:
                upstream_id=int(item['stream_id']);title=str(item.get('name','')).strip()
                if not title:continue
                ext=str(item.get('container_extension') or 'mp4').lower()
                if ext not in ('mp4','mkv','avi','ts'):continue
                sid=stable_id('xtream-movie|'+str(upstream_id))
                while sid in murls:sid=(sid%0x7fffffff)+1
                group='POWERDINO | '+movie_names.get(str(item.get('category_id')),'MOVIES')
                mcats.setdefault(group,str(800000+len(mcats)))
                mrows.append({'num':len(mrows)+1,'name':title,'stream_type':'movie','stream_id':sid,
                    'stream_icon':item.get('stream_icon',''),'rating':'','rating_5based':0,
                    'added':str(item.get('added','0')),'category_id':mcats[group],
                    'container_extension':ext,'custom_sid':'','direct_source':''})
                minfo[sid]={'info':{'name':title,'movie_image':item.get('stream_icon',''),
                    'plot':'','genre':'','releasedate':'','rating':''},
                    'movie_data':{'stream_id':sid,'name':title,'container_extension':ext}}
                murls[sid]=('movie',upstream_id,ext)
            except (KeyError,ValueError,TypeError):continue
        for item in series[:XTREAM_SERIES_LIMIT]:
            try:
                upstream_id=int(item['series_id']);title=str(item.get('name','')).strip()
                if not title:continue
                sid=stable_id('xtream-series|'+str(upstream_id))
                group='POWERDINO | '+series_names.get(str(item.get('category_id')),'SERIES')
                scats.setdefault(group,str(810000+len(scats)))
                details=xtream_api('get_series_info',series_id=upstream_id)
                episodes={};seasons=[]
                for season,eps in (details.get('episodes') or {}).items():
                    if not isinstance(eps,list):continue
                    rows=[]
                    for ep in eps:
                        try:
                            eid0=int(ep['id']);ext=str(ep.get('container_extension') or 'mp4').lower()
                            if ext not in ('mp4','mkv','avi','ts'):continue
                            eid=stable_id('xtream-episode|'+str(eid0))
                            while eid in surls:eid=(eid%0x7fffffff)+1
                            surls[eid]=('series',eid0,ext)
                            rows.append({'id':str(eid),'episode_num':int(ep.get('episode_num') or len(rows)+1),
                                'title':str(ep.get('title') or 'Episode'), 'container_extension':ext,
                                'info':{'movie_image':item.get('cover',''),'plot':''}})
                        except (ValueError,KeyError,TypeError):continue
                    if rows:
                        episodes[str(season)]=rows
                        seasons.append({'season_number':int(season),'name':'Season '+str(season),
                            'episode_count':len(rows),'cover':item.get('cover','')})
                if not episodes:continue
                sinfo[sid]={'info':details.get('info') or {'name':title},'seasons':seasons,'episodes':episodes}
                srows.append({'num':len(srows)+1,'name':title,'series_id':sid,'cover':item.get('cover',''),
                    'plot':item.get('plot',''),'genre':item.get('genre',''),
                    'releaseDate':item.get('releaseDate',''),'rating':'','category_id':scats[group]})
            except (ValueError,KeyError,TypeError):continue
        for item in live[:XTREAM_LIVE_LIMIT]:
            try:
                upstream_id=int(item['stream_id']);title=str(item.get('name','')).strip()
                if not title:continue
                sid=stable_id('xtream-live|'+str(upstream_id))
                while sid in lurls:sid=(sid%0x7fffffff)+1
                group='POWERDINO | LIVE'
                lcats.setdefault(group,'820000')
                lrows.append({'num':len(lrows)+1,'name':title,'stream_type':'live','stream_id':sid,
                    'stream_icon':item.get('stream_icon',''),'epg_channel_id':'','added':'0',
                    'category_id':lcats[group],'custom_sid':'','tv_archive':0,'direct_source':'',
                    'tv_archive_duration':0,'container_extension':'ts'})
                lurls[sid]=('live',upstream_id,'ts')
            except (KeyError,ValueError,TypeError):continue
        with LOCK:
            UPSTREAM['movie']=murls;UPSTREAM['series']=surls;UPSTREAM['live']=lurls
            UPSTREAM_META.update(status='ready',movies=len(murls),series=len(sinfo),live=len(lurls))
            VOD['movies']=VOD['movies']+mrows
            VOD['movie_info']={**VOD['movie_info'],**minfo}
            VOD['movie_categories']=VOD['movie_categories']+[
                {'category_id':v,'category_name':k,'parent_id':0} for k,v in mcats.items()]
            VOD['series']=VOD['series']+srows
            VOD['series_info']={**VOD['series_info'],**sinfo}
            VOD['series_categories']=VOD['series_categories']+[
                {'category_id':v,'category_name':k,'parent_id':0} for k,v in scats.items()]
            STATE['streams']=STATE['streams']+lrows
            STATE['categories']=STATE['categories']+[
                {'category_id':v,'category_name':k,'parent_id':0} for k,v in lcats.items()]
        print('OG IPTV Xtream catalog ready',UPSTREAM_META['movies'],UPSTREAM_META['series'],flush=True)
    except Exception as exc:
        with LOCK:UPSTREAM_META.update(status='error',error=type(exc).__name__)
        print('Xtream catalog error',type(exc).__name__,flush=True)


def background_xtream():
    # Wait for initial independent scans so their snapshots are not overwritten.
    for _ in range(900):
        with LOCK:done=(not VOD['checking'] and VOD['at'] and (not XTREAM_LIVE_LIMIT or (not STATE['checking'] and STATE['at'])))
        if done:break
        time.sleep(2)
    xtream_catalog()

class Handler(BaseHTTPRequestHandler):
    def reply(self,obj,status=200):
        body=json.dumps(obj,ensure_ascii=False).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        if self.command!='HEAD':
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
    def proxy_upstream(self, kind, upstream_id, ext):
        if not XTREAM_READY:return self.reply({'error':'Upstream disabled'},503)
        # Upstream is configured by the server operator, not by the requester.
        url=(XTREAM_URL+'/'+kind+'/'+urllib.parse.quote(XTREAM_USER,safe='')+'/'+
             urllib.parse.quote(XTREAM_PASSWORD,safe='')+'/'+str(upstream_id)+'.'+ext)
        headers={'User-Agent':'OG-IPTV/3.7'}
        if self.headers.get('Range'):headers['Range']=self.headers['Range']
        try:
            req=urllib.request.Request(url,headers=headers)
            with urllib.request.urlopen(req,timeout=25) as upstream:
                code=upstream.status
                self.send_response(code)
                for header in ('Content-Type','Content-Length','Content-Range','Accept-Ranges'):
                    value=upstream.headers.get(header)
                    if value:self.send_header(header,value)
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                if self.command!='HEAD':
                    while True:
                        chunk=upstream.read(65536)
                        if not chunk:break
                        self.wfile.write(chunk)
        except (BrokenPipeError,ConnectionResetError):pass
        except urllib.error.HTTPError as exc:self.reply({'error':'Upstream HTTP '+str(exc.code)},502)
        except Exception:self.reply({'error':'Upstream unavailable'},502)
    def do_HEAD(self):self.reply({'ok':True})
    def do_GET(self):
        parsed=urllib.parse.urlsplit(self.path);path=parsed.path
        params=urllib.parse.parse_qs(parsed.query);get=lambda k:params.get(k,[''])[0]
        with LOCK:live=dict(STATE);vod=dict(VOD)
        if path in ('/','/health'):
            return self.reply({'ok':True,'name':'OG IPTV','version':'3.7',
                'streams_loaded':len(live['streams']),'last_scan':int(live['at']),
                'scanning':live['checking'],'scan_error':live['error'],
                'scan_checked':live['checked'],'scan_total':live['total'],
                'scan_remaining':max(0,live['total']-live['checked']),
                'categories_loaded':len(live['categories']),
                'adult_streams_loaded':sum(s['category_id']=='1' for s in live['streams']),
                'adult_playlist_configured':bool(ADULT_PLAYLIST_URL),
                'minimum_verified_height':MIN_HEIGHT,
                'vod_scanning':vod['checking'],'vod_error':vod['error'],
                'movies_loaded':len(vod['movie_info']),'series_loaded':len(vod['series_info']),
                'movie_categories_loaded':len(vod['movie_categories']),
                'series_categories_loaded':len(vod['series_categories']),
                'vod_last_scan':int(vod['at']), 'xtream':dict(UPSTREAM_META)})
        if path=='/diagnostics':
            if get('username')!=USER or get('password')!=PASSWORD:return self.reply({'error':'Unauthorized'},401)
            return self.reply({'version':'3.7','stats':live['stats'],'checked':live['checked'],
                'total':live['total'],'accepted_total':len(live['streams']),
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
            upstream=UPSTREAM.get(kind,{}).get(sid)
            if upstream:return self.proxy_upstream(*upstream)
            target=(live['urls'] if kind=='live' else vod['movie_urls'] if kind=='movie' else vod['series_urls']).get(sid)
            if not target:return self.reply({'error':'Unknown stream'},404)
            self.send_response(302);self.send_header('Location',target)
            self.send_header('Cache-Control','no-store');self.end_headers();return
        if path not in ('/player_api.php','/get.php'):return self.reply({'error':'Not found'},404)
        if get('username')!=USER or get('password')!=PASSWORD:
            return self.reply({'user_info':{'auth':0,'status':'Disabled'}},401)
        if path=='/get.php':
            lines=['#EXTM3U'];cats={c['category_id']:c['category_name'] for c in live['categories']}
            for s in live['streams']:
                group=cats.get(s['category_id'],'OTHER')
                target=(PUBLIC_URL+'/live/'+urllib.parse.quote(USER,safe='')+'/'+
                        urllib.parse.quote(PASSWORD,safe='')+'/'+str(s['stream_id'])+'.m3u8')
                lines.extend([f'#EXTINF:-1 tvg-logo="{s["stream_icon"]}" group-title="{group}",{s["name"]}',target])
            body='\n'.join(lines).encode('utf-8')
            self.send_response(200);self.send_header('Content-Type','application/x-mpegURL')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
        action=get('action');cid=get('category_id')
        if action=='get_live_categories':return self.reply(live['categories'])
        if action=='get_live_streams':return self.reply([s for s in live['streams'] if not cid or s['category_id']==cid])
        if action=='get_vod_categories':return self.reply(vod['movie_categories'])
        if action=='get_vod_streams':return self.reply([s for s in vod['movies'] if not cid or s['category_id']==cid])
        if action=='get_vod_info':
            try:sid=int(get('vod_id'))
            except ValueError:return self.reply({})
            return self.reply(vod['movie_info'].get(sid,{}))
        if action=='get_series_categories':return self.reply(vod['series_categories'])
        if action=='get_series':return self.reply([s for s in vod['series'] if not cid or s['category_id']==cid])
        if action=='get_series_info':
            try:sid=int(get('series_id'))
            except ValueError:return self.reply({})
            return self.reply(vod['series_info'].get(sid,{}))
        if action in ('get_short_epg','get_simple_data_table'):return self.reply({'epg_listings':[]})
        public=urllib.parse.urlsplit(PUBLIC_URL);secure=public.scheme=='https'
        return self.reply({'user_info':{'username':USER,'password':PASSWORD,'auth':1,
            'status':'Active','exp_date':None,'is_trial':'0','active_cons':'0','max_connections':'1',
            'allowed_output_formats':['m3u8','mp4','mkv']},
            'server_info':{'url':public.hostname,'port':str(public.port or (443 if secure else 80)),
            'https_port':'443' if secure else '', 'server_protocol':public.scheme,
            'timezone':'Europe/Zurich','timestamp_now':int(time.time())}})

if __name__=='__main__':
    Thread(target=scan_live,daemon=True).start()
    Thread(target=scan_vod,daemon=True).start()
    if XTREAM_READY:Thread(target=background_xtream,daemon=True).start()
    print('OG IPTV v3.7 running on',HOST,PORT,flush=True)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
