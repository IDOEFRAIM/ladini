import asyncio


def test_rag_service_search_documents_formats_payload():
    from agriconnect.services.rag_service import RagService

    class Node:
        def __init__(self):
            self.metadata = {"source": "doc1.txt", "title": "Doc 1"}

        def get_content(self):
            return "Irrigation du riz et gestion de l'eau."

    class ScoredNode:
        def __init__(self):
            self.node = Node()
            self.score = 0.82

    class FakeRetriever:
        def search(self, query, user_level="debutant"):
            return [ScoredNode()]

        def search_memory(self, query, user_id, top_k=3):
            return []

    service = RagService(retriever=FakeRetriever())
    payload = asyncio.run(service.search_documents(query="type de riz", level="debutant", top_k=4))

    assert payload["query"] == "type de riz"
    assert payload["total_found"] == 1
    assert payload["documents"][0]["title"] == "Doc 1"
    assert payload["documents"][0]["source"] == "doc1.txt"


def test_rag_service_search_memory_formats_episode_list():
    from agriconnect.services.rag_service import RagService

    class FakeRetriever:
        def search(self, query, user_level="debutant"):
            return []

        def search_memory(self, query, user_id, top_k=3):
            return [{"id": 12, "text": "memo", "category": "advice", "score": 0.7}]

    service = RagService(retriever=FakeRetriever())
    episodes = asyncio.run(service.search_memory(user_id="u1", query="riz", top_k=3))

    assert len(episodes) == 1
    assert episodes[0]["episode_id"] == "12"
    assert episodes[0]["user_id"] == "u1"
    assert episodes[0]["summary"] == "memo"


def test_rag_service_search_documents_handles_invalid_query():
    from agriconnect.services.rag_service import RagService

    class FakeRetriever:
        def search(self, query, user_level="debutant"):
            return [{"text": "x", "metadata": {}, "score": 0.1}]

        def search_memory(self, query, user_id, top_k=3):
            return []

    service = RagService(retriever=FakeRetriever())
    payload = asyncio.run(service.search_documents(query="   ", level="debutant", top_k=4))

    assert payload["status"] == "invalid_query"
    assert payload["total_found"] == 0


def test_rag_service_search_documents_handles_retriever_error():
    from agriconnect.services.rag_service import RagService

    class FailingRetriever:
        def search(self, query, user_level="debutant"):
            raise RuntimeError("boom")

        def search_memory(self, query, user_id, top_k=3):
            return []

    service = RagService(retriever=FailingRetriever())
    payload = asyncio.run(service.search_documents(query="riz", level="expert", top_k=4))

    assert payload["status"] == "error"
    assert payload["total_found"] == 0


def test_rag_service_search_memory_empty_query_returns_empty_list():
    from agriconnect.services.rag_service import RagService

    class FakeRetriever:
        def search(self, query, user_level="debutant"):
            return []

        def search_memory(self, query, user_id, top_k=3):
            raise AssertionError("Should not be called for empty query")

    service = RagService(retriever=FakeRetriever())
    episodes = asyncio.run(service.search_memory(user_id="u1", query="   ", top_k=3))

    assert episodes == []
