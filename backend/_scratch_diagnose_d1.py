import asyncio
import json

from ladini.graphs.agents.market_coach.domain.selection_actions import (
    ActionType, SelectionContext, TierOption,
)
from ladini.graphs.agents.market_coach.interpreter.structured_action_contract import (
    build_structured_action_prompt_context,
)
from ladini.graphs.agents.market_coach.interpreter.structured_action_prompts import (
    build_structured_action_system_prompt, build_structured_action_user_prompt,
    build_structured_action_repair_prompt,
)


class _RealGatewayRuntime:
    @property
    def llm_gateway(self):
        from ladini.graphs.agents.market_coach.llm_gateway import get_llm_gateway
        return get_llm_gateway()


TIERS = [
    TierOption(tier_id="T5", label="5.0 L (bidon) — 450.0 FCFA"),
    TierOption(tier_id="T10", label="10.0 L (bidon) — 800.0 FCFA"),
    TierOption(tier_id="T20", label="20.0 L (bidon) — 1500.0 FCFA"),
]
CTX = SelectionContext(expected_action=ActionType.SET_PACKAGE_COUNT, tier_options=TIERS, active_tier_id="T5")


async def main():
    from ladini.graphs.agents.market_coach.llm_gateway import LLMProfile, resolve_gateway

    runtime = _RealGatewayRuntime()
    pc = build_structured_action_prompt_context(CTX)
    system_prompt = build_structured_action_system_prompt()
    user_prompt = build_structured_action_user_prompt(prompt_context=pc, goal="BUYER_ADD_TO_CART", normalized_text="finalement le bidon de 10 L")
    print("=== SYSTEM ===")
    print(system_prompt)
    print("\n=== USER ===")
    print(user_prompt)

    gateway = resolve_gateway(runtime)
    for attempt in range(3):
        completion = await gateway.complete(
            profile=LLMProfile.INTERPRETER,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            response_format={"type": "json_object"}, temperature=0.0, max_tokens=600,
        )
        raw = completion.choices[0].message.content
        print(f"\n=== RAW (attempt {attempt+1}, model={completion.model}) ===")
        print(raw)
        await asyncio.sleep(2.5)


if __name__ == "__main__":
    asyncio.run(main())
