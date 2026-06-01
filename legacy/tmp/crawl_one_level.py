import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

HEADERS = {"User-Agent": "AgriConnectBot/1.0 (+https://example.org)"}

INPUT = 'tmp/extract_pdf_links_simple_output.json'
OUTPUT = 'tmp/crawl_one_level_output.json'


def extract_pdf_hrefs(html, base_url):
    soup = BeautifulSoup(html, 'html.parser')
    found = []
    for a in soup.find_all('a', href=True):
        href = urljoin(base_url, a['href'])
        if '.pdf' in href.lower():
            found.append({'url': href, 'anchor_text': (a.get_text() or '').strip()})
    for e in soup.find_all(['embed','iframe'], src=True):
        src = urljoin(base_url, e['src'])
        if '.pdf' in src.lower():
            found.append({'url': src, 'anchor_text': ''})
    for o in soup.find_all('object'):
        data = o.get('data')
        if data:
            u = urljoin(base_url, data)
            if '.pdf' in u.lower():
                found.append({'url': u, 'anchor_text': ''})
        for p in o.find_all('param'):
            v = p.get('value')
            if v and '.pdf' in v.lower():
                found.append({'url': urljoin(base_url, v), 'anchor_text': ''})
    return found


def crawl_one_level():
    with open(INPUT, 'r', encoding='utf-8') as f:
        roots = json.load(f)
    results = []
    for root in roots:
        source_url = root.get('source_url')
        candidates = [c['url'] for c in root.get('pdf_candidates', [])]
        # also include the root page itself as candidate
        to_visit = []
        for u in candidates:
            to_visit.append(u)
        # dedupe
        to_visit = list(dict.fromkeys(to_visit))
        collected = []
        for u in to_visit:
            try:
                r = requests.get(u, timeout=15, headers=HEADERS)
                if r.status_code != 200:
                    continue
                found = extract_pdf_hrefs(r.text, u)
                if found:
                    collected.append({'parent': u, 'found_pdfs': found})
            except Exception as e:
                # record error per candidate
                collected.append({'parent': u, 'error': str(e)})
        results.append({'root': source_url, 'crawled_candidates_count': len(to_visit), 'collected': collected})
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print('Wrote', OUTPUT)

if __name__ == '__main__':
    crawl_one_level()
