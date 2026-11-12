#!/usr/bin/env python3
"""Build a bounded, licensed Internet Archive VOD feed outside Render.
Only metadata explicitly marked CC0/Public Domain Mark is eligible.
No downloads of video, no scraping of commercial streaming services.
"""
import json, re, time, urllib.parse, urllib.request, pathlib, concurrent.futures

OUT = pathlib.Path('external_movies.json')
MAX_ITEMS = 100
SEARCH_PER_QUERY = 90
UA = 'OG-IPTV-catalog/1.0 (metadata discovery; contact via repository)'
QUERIES = [
    'mediatype:movies AND language:(per OR fas OR fa OR persian OR farsi) AND (licenseurl:creativecommons.org/publicdomain/zero/* OR licenseurl:creativecommons.org/publicdomain/mark/*)',
    'mediatype:movies AND (title:iranian OR title:persian OR subject:iran OR subject:persian) AND (licenseurl:creativecommons.org/publicdomain/zero/* OR licenseurl:creativecommons.org/publicdomain/mark/*)',
    'mediatype:movies AND (licenseurl:creativecommons.org/publicdomain/zero/* OR licenseurl:creativecommons.org/publicdomain/mark/*)',
]

def fetch(url, limit=3_000_000):
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=24) as response:
        raw=response.read(limit+1)
    if len(raw)>limit:raise ValueError('metadata response too large')
    return json.loads(raw)

def search(query):
    args=urllib.parse.urlencode({'q':query,'fl[]':['identifier','title','language','subject'],'rows':SEARCH_PER_QUERY,'page':1,'output':'json'},doseq=True)
    return fetch('https://archive.org/advancedsearch.php?'+args).get('response',{}).get('docs',[])

def scalar(value):
    if isinstance(value,list):return str(value[0]) if value else ''
    return str(value or '')

def eligible(doc):
    ident=scalar(doc.get('identifier'))
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}',ident):return None
    data=fetch('https://archive.org/metadata/'+urllib.parse.quote(ident),limit=5_000_000)
    md=data.get('metadata') or {}
    license_url=scalar(md.get('licenseurl')).lower().rstrip('/')
    if not (license_url.startswith('https://creativecommons.org/publicdomain/zero/1.0') or
            license_url.startswith('http://creativecommons.org/publicdomain/zero/1.0') or
            license_url.startswith('https://creativecommons.org/publicdomain/mark/1.0') or
            license_url.startswith('http://creativecommons.org/publicdomain/mark/1.0')):return None
    if scalar(md.get('mediatype')).lower()!='movies':return None
    files=[]
    for f in data.get('files',[]):
        name=scalar(f.get('name'))
        if f.get('private') or f.get('is_dark'):continue
        if name.lower().endswith(('.mp4','.webm','.mkv')) and '/' not in name.lstrip('/'):
            try:size=int(f.get('size') or 0)
            except (TypeError,ValueError):size=0
            if size>=2_000_000:files.append((f.get('source')=='original',size,name))
    if not files:return None
    files.sort(reverse=True)
    filename=files[0][2]
    title=scalar(md.get('title') or doc.get('title') or ident).strip()
    if not title:return None
    lang=scalar(md.get('language') or doc.get('language'))
    subject=scalar(md.get('subject') or doc.get('subject'))
    persian=bool(re.search(r'(?i)(persian|farsi|iran|فارسی|ایران|دری|dari|\bfas\b|\bper\b)',title+' '+lang+' '+subject))
    return {'title':title[:200], 'url':'https://archive.org/download/'+urllib.parse.quote(ident,safe='')+'/'+urllib.parse.quote(filename,safe='/'),
        'language':'fa' if persian else lang[:60], 'genre':'Persian / Iranian' if persian else 'Archive',
        'poster':'https://archive.org/services/img/'+urllib.parse.quote(ident,safe=''),
        'description':('Public-domain/CC0 metadata: '+license_url)[:300],
        'source':'Internet Archive (license metadata checked)', 'license_url':license_url,
        'license_page':'https://archive.org/details/'+urllib.parse.quote(ident,safe='')}

def main():
    docs=[];seen=set()
    for q in QUERIES:
        try:
            for d in search(q):
                ident=scalar(d.get('identifier'))
                if ident not in seen:docs.append(d);seen.add(ident)
        except Exception as e:print('Search failed:',type(e).__name__,str(e)[:100])
    results=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(eligible,d) for d in docs[:240]]
        for f in concurrent.futures.as_completed(futures):
            try:
                item=f.result()
                if item:results.append(item)
            except Exception:pass
    results.sort(key=lambda x:(not bool(re.search(r'(?i)(persian|farsi|iran|فارسی|ایران|دری)',x['title']+' '+x['genre'])),x['title'].casefold()))
    results=results[:MAX_ITEMS]
    # Preserve last good catalog when discovery is temporarily unavailable.
    if not results and OUT.exists():
        print('No verified results; preserving previous catalog');return
    OUT.write_text(json.dumps(results,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    print('Wrote',len(results),'license-filtered films; Persian metadata',sum(x['language']=='fa' for x in results))
    print('NOTE: metadata licensing does not independently prove uploader ownership or playback availability.')

if __name__=='__main__':main()
