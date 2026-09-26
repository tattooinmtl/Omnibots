"""The provider lineup (PLAN.md ADR-11, decided by the user 2026-09-25).

Names are Omni's provider and model keys, so they match ~/.omni/agent/settings.json.
"""

MINIMAX = "minimax.io"                                    # 4 seats, primary
MINIMAX_SEATS = 4
MINIMAX_TOKEN_BUDGET = 1_500_000_000                      # the user's 1.5B-token reservoir
CHEAP_LANE = ["nvidia", "agnes", "openrouter", "xkiro"]   # the 5th bot's failover chain
# atria (Atria-Dawn-Preview) removed by the user 2026-09-25: 66–112 s to first token.
# Providers A1 checks for: the lineup plus groq, which Omni also has a key for.
TARGET_PROVIDERS = [MINIMAX, *CHEAP_LANE, "groq"]

# One proven model per provider (Omni model keys). Mostly the ones Omni itself
# uses for failover; configurable later (A7 Bot Factory / parameters).
LINEUP_MODELS = {
    "minimax.io": "minimax.io/m3",                     # MiniMax-M3, 1M context
    "nvidia": "nvidia/nemotron-3-ultra-550b-a55b",
    "agnes": "agnes/agnes-2.5-flash",
    # Omni's saved openrouter/groq models are retired (404 on 2026-09-25), so
    # these name live models directly with `provider::model-id` (client.resolve_model).
    "openrouter": "openrouter::nvidia/nemotron-3-super-120b-a12b:free",
    "xkiro": "xkiro/mistral-medium-3.5",
    "groq": "groq::openai/gpt-oss-120b",
}

# Chains of model keys, tried in order by the router (A2.b.04).
MINIMAX_FIRST = [LINEUP_MODELS[MINIMAX], *(LINEUP_MODELS[p] for p in CHEAP_LANE)]
CHEAP_FIRST = [*(LINEUP_MODELS[p] for p in CHEAP_LANE), LINEUP_MODELS[MINIMAX]]
