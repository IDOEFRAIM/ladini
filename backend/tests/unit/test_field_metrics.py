"""KPI conversationnels préparés : répétition utilisateur et taux de réparation."""
from __future__ import annotations

from tests.field_corpus.metrics import conversation_repair_rate, user_repetition_rate


def test_a_user_who_repeats_the_same_request_is_counted():
    assert user_repetition_rate(["Je veux du lait", "le moins cher", "je veux du lait"]) == 1 / 3
    assert user_repetition_rate(["Je veux du lait", "le quatrième", "10 litres"]) == 0.0
    assert user_repetition_rate(["bonjour"]) == 0.0


def test_the_repair_rate_counts_only_dont_understand_replies():
    replies = ["Je n'ai pas bien compris ton message.", "Moussa est le moins cher.", "Je ne comprends pas bien votre demande."]
    assert conversation_repair_rate(replies) == 2 / 3
    assert conversation_repair_rate([]) == 0.0
