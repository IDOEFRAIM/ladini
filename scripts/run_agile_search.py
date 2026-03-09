from agriconnect.rag.retriever import AgileRetriever

def main():
    r = AgileRetriever()
    print('Instantiated AgileRetriever')
    res = r.search('physionomie du mil', user_level='debutant')
    print('Found', len(res))
    for n in res[:3]:
        try:
            print('---', n.node.get_content()[:200])
        except Exception:
            print('raw', n)

if __name__ == '__main__':
    main()
