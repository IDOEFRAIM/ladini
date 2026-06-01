import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import json

HEADERS = {"User-Agent": "AgriConnectBot/1.0 (+https://example.org)"}

def extract_candidates(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for a in soup.find_all('a', href=True):
        candidates.append(('anchor', urljoin(base_url, a['href']), (a.get_text() or '').strip()))
    for it in soup.find_all('iframe', src=True):
        candidates.append(('iframe', urljoin(base_url, it['src']), ''))
    for e in soup.find_all('embed', src=True):
        candidates.append(('embed', urljoin(base_url, e['src']), ''))
    for o in soup.find_all('object'):
        data = o.get('data')
        if data:
            candidates.append(('object', urljoin(base_url, data), ''))
        for p in o.find_all('param'):
            if p.get('name','').lower() in ('src','data','movie','file') and p.get('value'):
                candidates.append(('object-param', urljoin(base_url, p['value']), ''))
    for m in soup.find_all('meta'):
        if m.get('http-equiv','').lower() == 'refresh' and m.get('content'):
            parts = m['content'].split(';')
            for part in parts:
                part = part.strip()
                if part.lower().startswith('url='):
                    candidates.append(('meta-refresh', urljoin(base_url, part[4:]), ''))
    return candidates


def analyze(url):
    out = {'source_url': url, 'page': {}, 'pdf_candidates': []}
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        out['page']['status_code'] = r.status_code
        out['page']['content_type'] = r.headers.get('Content-Type')
        candidates = extract_candidates(r.text, url)
        seen = set()
        for kind, absu, anchor_text in candidates:
            if absu in seen:
                continue
            seen.add(absu)
            cand = {'kind': kind, 'url': absu, 'anchor_text': anchor_text}
            cand['contains_pdf'] = ('.pdf' in absu.lower())
            out['pdf_candidates'].append(cand)
    except Exception as e:
        out['error'] = str(e)
    return out

if __name__ == '__main__':
    targets = [
        'https://www.fao.org/newsroom/en/',
        'https://meteoburkina.bf/produits/bulletin-agrometeologique-decadaire/'
    ]
    results = []
    for t in targets:
        print(f'--- Analyzing {t} ---')
        res = analyze(t)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        results.append(res)
    with open('tmp/extract_pdf_links_simple_output.json','w',encoding='utf-8') as f:
        json.dump(results,f,indent=2,ensure_ascii=False)
    print('\nWrote tmp/extract_pdf_links_simple_output.json')
