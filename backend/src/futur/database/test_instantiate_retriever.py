import logging
import traceback
from futur.rag.retriever import AgileRetriever

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('test')
try:
    r = AgileRetriever()
    print('INST_SUCCESS index=', bool(getattr(r,'index',None)))
    print('vector_store present=', bool(getattr(r,'vector_store',None)))
except Exception as e:
    print('EXC during AgileRetriever init')
    traceback.print_exc()
