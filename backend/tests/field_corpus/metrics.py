"""KPI de robustesse conversationnelle, calculables à partir d'une transcription (logs ou corpus) — préparés, pas encore branchés à un tableau de bord.

- `user_repetition_rate`   : part des messages utilisateur qui REDISENT (à peu près) un message précédent — l'utilisateur répète parce qu'il n'a pas été compris.
- `conversation_repair_rate`: part des tours où Ladini n'a pas compris (réponse de repli/clarification générique) et où l'utilisateur doit reformuler.

Ils se dérivent des journaux structurés (`interaction_mode`, `clarification_reason`, `relation_type`) : aucun nouveau stockage.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Sequence


def _norm(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")
    return {t for t in re.findall(r"[a-z0-9]+", folded) if len(t) > 2}


def _similar(a: set[str], b: set[str]) -> bool:
    return bool(a and b) and len(a & b) / min(len(a), len(b)) >= 0.75


def user_repetition_rate(user_messages: Sequence[str], window: int = 4) -> float:
    """Part des messages qui répètent l'un des `window` messages précédents (« Je veux du lait » ... « je veux du lait »)."""
    if len(user_messages) < 2:
        return 0.0
    tokens: List[set[str]] = [_norm(m) for m in user_messages]
    repeats = sum(1 for i in range(1, len(tokens)) if any(_similar(tokens[i], tokens[j]) for j in range(max(0, i - window), i)))
    return repeats / len(user_messages)


_FALLBACK_MARKERS = ("pas bien compris", "ne comprends pas", "pas sûr de comprendre", "n'ai pas compris", "pas compris")


def conversation_repair_rate(assistant_replies: Iterable[str]) -> float:
    """Part des réponses qui sont un « je n'ai pas compris » (l'utilisateur doit reformuler)."""
    replies = list(assistant_replies)
    if not replies:
        return 0.0
    return sum(1 for r in replies if any(m in r.lower() for m in _FALLBACK_MARKERS)) / len(replies)
