import os, psycopg2
if os.path.exists('backend/.env'):
    with open('backend/.env') as f:
        for l in f:
            if '=' in l:
                k,v=l.split('=',1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
DB=os.environ.get('DATABASE_URL')
conn=psycopg2.connect(DB)
cur=conn.cursor()
cur.execute("SELECT chunk_id, doc_ref, content_md5, chunk_version FROM agri_vector.document_chunks WHERE content_md5 IN ('9e26b084ad383ad0103b6bc42734362d','2567ec3bb8b291e4d693c4ab01c65c51','f678e916d201c366ced1acbae97d113f')")
rows=cur.fetchall()
print('found:',rows)
cur.close();conn.close()
