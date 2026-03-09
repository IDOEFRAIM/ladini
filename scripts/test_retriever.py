import json
import traceback
import sys


def main():
    try:
        from agriconnect.rag.retriever import AgileRetriever
        print('AgileRetriever import OK')
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    try:
        r = AgileRetriever()
        print('AgileRetriever instantiated')
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    try:
        res = r.search('physionomie du mil', user_level='debutant')
        print('search() type:', type(res))
        try:
            print('search() repr:', repr(res)[:2000])
        except Exception as e:
            print('repr failed', e)
    except Exception:
        print('search() raised:')
        traceback.print_exc()

    try:
        mem = r.search_memory('test_user', query='mil', top_k=3)
        print('search_memory() type:', type(mem))
        try:
            print('search_memory() repr:', repr(mem)[:2000])
        except Exception as e:
            print('repr failed', e)
    except Exception:
        print('search_memory() raised:')
        traceback.print_exc()


if __name__ == '__main__':
    main()
