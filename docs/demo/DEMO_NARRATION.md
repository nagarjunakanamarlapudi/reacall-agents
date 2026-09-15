# RecallOps narrated demo

This is the voice script for `RecallOps-LLM-Only-Demo.mp4`. The recording shows the real OpenAI `gpt-5-mini` provider path and never substitutes a control fixture for that run.

## Storyboard

| Scene | Screen | Demonstrates |
|---|---|---|
| 1 | Command Center | Product, business problem, and real OpenAI mode |
| 2 | High-level architecture | LLM planning, Deep Agents, specialists, verifier, HITL, MCP, LangGraph |
| 3 | Four-lot scope | Official/synthetic provenance and four of 144 selected lots |
| 4 | Live run | Observed plan, MCP reads, middleware, and tool isolation |
| 5 | Actual outcome | Provider telemetry and strict fail-closed behavior |
| 6 | RAG trace | Sparse/dense fusion, rerank, critic, rewrite, and citations |
| 7 | Human Review | HITL is unreachable when verification fails |
| 8 | Evaluation | 21 safety scenarios and 96 retrieval cases across six configurations |
| 9 | Evaluation | RAG ablations and 24 orchestration cases across two profiles |
| 10 | Architecture close | Complete Week 3 concept map and conclusion |

## Demonstration truth

- Model: OpenAI `gpt-5-mini`.
- Live result: the four-role plan was observed; recall intelligence completed; product/lot matching failed its strict typed contract after one bounded correction.
- Safe result: independent verification failed, the graph escalated, no HITL packet was minted, and zero writes occurred.
- Evaluation: committed offline gates report 21/21 safety scenarios, 96 retrieval cases × 6 configurations, and 24 orchestration cases × 2 profiles.
- Credentials: `.env` is ignored and no API key is shown or embedded in the video.
