class DimensionMismatchError(Exception):
    """Raised when an embedding vector does not match the expected RAG dimension."""

    def __init__(self, expected: int, actual: int):
        super().__init__(f"Embedding dimension mismatch: expected={expected}, actual={actual}")
        self.expected = expected
        self.actual = actual
