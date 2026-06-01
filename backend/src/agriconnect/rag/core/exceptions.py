class RAGError(Exception):
    pass


class SynthesisInconsistencyError(RAGError):
    """Raised when synthesizer detects source/answer inconsistency."""


class StrategyExecutionError(RAGError):
    pass
