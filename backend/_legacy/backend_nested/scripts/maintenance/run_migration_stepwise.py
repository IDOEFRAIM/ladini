import os, sys, re
from pathlib import Path

def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        k,v = line.split('=',1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

def split_statements(sql_text: str):
    # Robust splitter: iterate characters and split on semicolons that are
    # not inside single/double quotes, dollar-quoted strings, or comments.
    parts = []
    buf = []
    i = 0
    s = sql_text
    L = len(s)
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    in_dollar = False
    dollar_tag = None

    def startswith_at(idx, prefix):
        return s.startswith(prefix, idx)

    while i < L:
        ch = s[i]
        # handle line comments --
        if not in_single and not in_double and not in_block_comment and not in_dollar:
            if startswith_at(i, '--'):
                in_line_comment = True
                buf.append('--')
                i += 2
                continue
        if in_line_comment:
            buf.append(ch)
            if ch == '\n':
                in_line_comment = False
            i += 1
            continue

        # handle block comments /* */
        if not in_single and not in_double and not in_dollar:
            if startswith_at(i, '/*'):
                in_block_comment = True
                buf.append('/*')
                i += 2
                continue
        if in_block_comment:
            if startswith_at(i, '*/'):
                buf.append('*/')
                i += 2
                in_block_comment = False
                continue
            else:
                buf.append(ch)
                i += 1
                continue

        # handle dollar-quoted tags like $tag$ ... $tag$
        if not in_single and not in_double and not in_block_comment:
            if not in_dollar and ch == '$':
                # attempt to read tag
                m = re.match(r"\$[A-Za-z0-9_]*\$", s[i:])
                if m:
                    dollar_tag = m.group(0)
                    in_dollar = True
                    buf.append(dollar_tag)
                    i += len(dollar_tag)
                    continue
            elif in_dollar and startswith_at(i, dollar_tag or '$$'):
                buf.append(dollar_tag or '$$')
                i += len(dollar_tag or '$$')
                in_dollar = False
                dollar_tag = None
                continue

        if in_dollar:
            buf.append(ch)
            i += 1
            continue

        # handle single/double quotes
        if ch == "'" and not in_double:
            buf.append(ch)
            # toggle single unless escaped
            if in_single:
                # check for escaped single by doubling
                if i+1 < L and s[i+1] == "'":
                    # it's an escaped quote; consume and keep in_single
                    buf.append("'")
                    i += 2
                    continue
                else:
                    in_single = False
                    i += 1
                    continue
            else:
                in_single = True
                i += 1
                continue

        if ch == '"' and not in_single:
            buf.append(ch)
            if in_double:
                # check for escaped double by doubling
                if i+1 < L and s[i+1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                else:
                    in_double = False
                    i += 1
                    continue
            else:
                in_double = True
                i += 1
                continue

        # semicolon splits only when not inside any quoting or comments
        if ch == ';' and not (in_single or in_double or in_block_comment or in_dollar or in_line_comment):
            buf.append(ch)
            stmt = ''.join(buf).strip()
            if stmt:
                parts.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = ''.join(buf).strip()
    if tail:
        parts.append(tail)
    return parts

def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    load_env(root / '.env')
    if len(sys.argv) < 2:
        print('Usage: run_migration_stepwise.py <sql_file>')
        sys.exit(1)
    fpath = Path(sys.argv[1])
    if not fpath.exists():
        print('File not found', fpath)
        sys.exit(1)
    try:
        import psycopg2
    except Exception as e:
        print('psycopg2 not available:', e)
        sys.exit(1)
    db = os.environ.get('DATABASE_URL')
    if not db:
        print('DATABASE_URL not set')
        sys.exit(1)
    conn = psycopg2.connect(db)
    conn.autocommit = True
    cur = conn.cursor()
    sql_text = fpath.read_text(encoding='utf-8')
    statements = split_statements(sql_text)
    print(f'Parsed {len(statements)} statements from {fpath}')
    for idx,st in enumerate(statements, start=1):
        preview = st.strip().splitlines()[0][:120] if st.strip() else '<empty>'
        print(f'  [{idx}] {preview[:120]}')
        print('    full:', repr(st))
    for i, stmt in enumerate(statements, start=1):
        stmt_strip = stmt.lstrip()
        first_line = stmt_strip.splitlines()[0] if stmt_strip else ''
        short = first_line[:120]
        # skip empty statements
        if not stmt_strip:
            print(f'[{i}/{len(statements)}] Skipping empty statement')
            continue
        # If statement starts with comments but also contains SQL, strip comment lines
        if stmt_strip.startswith('--'):
            lines = stmt.splitlines()
            # find first non-comment/non-empty line
            first_sql_idx = None
            for idx_line, ln in enumerate(lines):
                if ln.strip() == '':
                    continue
                if ln.lstrip().startswith('--'):
                    continue
                first_sql_idx = idx_line
                break
            if first_sql_idx is None:
                print(f'[{i}/{len(statements)}] Skipping comment-only statement: {short[:80]}')
                continue
            # rebuild statement as the SQL part (skip leading comment lines)
            stmt = '\n'.join(lines[first_sql_idx:])
            stmt_strip = stmt.strip()
            short = stmt_strip.splitlines()[0][:120]
        print(f'[{i}/{len(statements)}] Executing: {short}...')
        try:
            cur.execute(stmt)
            print('  ✅ OK')
        except Exception as e:
            print('  ⚠️ ERROR:', e)
            print('  >>> Statement that failed:\n', stmt)
    cur.close(); conn.close()

if __name__=='__main__':
    main()
