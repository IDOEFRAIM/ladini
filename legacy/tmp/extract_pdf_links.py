import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import json

HEADERS = {
    "User-Agent": "AgriConnectBot/1.0 (+https://example.org)"
}

def extract_candidates(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    # anchors
    for a in soup.find_all('a', href=True):
        candidates.append(('anchor', a['href'], (a.get_text() or '').strip()))
    # iframes
    for it in soup.find_all('iframe', src=True):
        candidates.append(('iframe', it['src'], ''))
    # embed
    for e in soup.find_all('embed', src=True):
        candidates.append(('embed', e['src'], ''))
    # object
    for o in soup.find_all('object'):
        data = o.get('data')
        if data:
            candidates.append(('object', data, ''))
        for p in o.find_all('param'):
            if p.get('name','').lower() in ('src','data','movie','file') and p.get('value'):
                candidates.append(('object-param', p['value'], ''))
    # meta refresh
    for m in soup.find_all('meta'):
        if m.get('http-equiv','').lower() == 'refresh' and m.get('content'):
            content = m['content']
            # typical format: '5; url=/path/file.pdf'
            parts = content.split(';')
            for part in parts:
                part = part.strip()
                if part.lower().startswith('url='):
                    candidates.append(('meta-refresh', part[4:], ''))
    return candidates


def check_head(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=15, headers=HEADERS)
        return r.status_code, r.headers.get('Content-Type')
    except Exception:
        return None, None


def analyze(url):
    out = {
        'source_url': url,
        'page': {},
        'pdf_candidates': []
    }
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        out['page']['status_code'] = r.status_code
        out['page']['content_type'] = r.headers.get('Content-Type')
        candidates = extract_candidates(r.text, url)
        seen = set()
        for kind, href, anchor_text in candidates:
            absu = urljoin(url, href)
            if absu in seen:
                continue
            seen.add(absu)
            candidate = {'kind': kind, 'url': absu, 'anchor_text': anchor_text}
            # quick heuristic
            low = absu.lower()
            if '.pdf' in low:
                candidate['heuristic'] = 'contains .pdf'
                sc, ct = check_head(absu)
                candidate['head_status_code'] = sc
                candidate['head_content_type'] = ct
                out['pdf_candidates'].append(candidate)
                continue
            # otherwise probe HEAD to see if it's a PDF
            sc, ct = check_head(absu)
            candidate['head_status_code'] = sc
            candidate['head_content_type'] = ct
            if ct and 'application/pdf' in ct.lower():
                candidate['heuristic'] = 'head says pdf'
                out['pdf_candidates'].append(candidate)
            else:
                candidate['heuristic'] = 'not-pdf'
            # still include non-pdf candidates in case they redirect to pdfs later
            out['pdf_candidates'].append(candidate)
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
    # also dump combined
    with open('tmp/extract_pdf_links_output.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print('\nWrote tmp/extract_pdf_links_output.json')
