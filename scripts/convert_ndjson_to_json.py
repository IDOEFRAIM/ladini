from pathlib import Path
import json

def convert(ndjson_path: str, json_path: str) -> int:
    p = Path(ndjson_path)
    out = Path(json_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        out.write_text('[]', encoding='utf-8')
        print('wrote empty', out)
        return 0
    lines = []
    with p.open('r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            lines.append(obj)
    out.write_text(json.dumps(lines, ensure_ascii=False, indent=2), encoding='utf-8')
    print('wrote', out, 'count', len(lines))
    return len(lines)

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print('usage: convert_ndjson_to_json.py <ndjson_path> <json_path>')
        raise SystemExit(2)
    nd = sys.argv[1]
    j = sys.argv[2]
    convert(nd, j)
