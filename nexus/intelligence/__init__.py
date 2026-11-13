"""Account 3 — intelligence layer: model routing, research, creator references, strategy, localisation intelligence,
publishing, analytics and growth experiments.

Sub-packages (one owner each, see docs/ACCOUNT3_HANDOFF_TO_INTEGRATION.md):
  contracts.py        shared documents exchanged with Accounts 1 and 2 (versioned, validated)
  model_router/       one provider-independent `complete()` for every AI task      (agent B)
  providers/          provider adapters (local llama.cpp, Ollama/vLLM/OpenAI-compatible, hosted-free) (agent B)
  capability_registry/ model + provider catalogue with licence, quota, privacy       (agent B)
  creators/           CreatorReferenceIntelligence                                   (agent A)
  market/             sources, opportunity scoring, research briefs                  (agent C)
  language/           localisation intelligence on top of nexus.localize             (agent D)
  publishing/         platform intelligence, publishing queue, analytics import      (agent E)
  growth/             experiments, learning loop, recommendations                    (agent F)
"""
