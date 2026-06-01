from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List

from .core.models import Node, QueryBundle


class BaseVectorStore(ABC):
    """Provider contract for vector backends.

    query() accepts QueryBundle and returns normalized Node list.
    Providers execute storage/query only; no business decisions.
    """

    dim: int

    @abstractmethod
    def add(self, doc_id: str, text: str, meta: Dict, embedding: List[float]) -> None:
        raise NotImplementedError

    @abstractmethod
    def add_many(self, items: List[Dict], batch_size: int = 50) -> None:
        raise NotImplementedError

    @abstractmethod
    def query(self, query_bundle: QueryBundle) -> List[Node]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, doc_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def health(self) -> Dict[str, object]:
        raise NotImplementedError
