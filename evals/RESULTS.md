# Eval results

One row per run of `forge eval` with `--report evals/RESULTS.md`: the model every role used,
the `PROMPTS_VERSION` of `src/forge/prompts.py`, the suite (`all`, or the `--suite` filter),
pass rate, cost and wall time. Live runs need API keys:

```bash
uv run forge eval --models anthropic/claude-sonnet,openai/gpt-5,gemini/gemini-3.8-flash,deepseek/deepseek-chat,groq/openai/gpt-oss-120b --report evals/RESULTS.md
uv run forge eval --swebench swe-bench-lite.jsonl --limit 50 --report evals/RESULTS.md
```

Offline runs (`--fake`) replay each task's scripted solution; they check the harness, not a model.


| date | model | prompts | suite | passed | pass rate | cost | time |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-05 | fake/scripted | 2026.10.4 | all | 33/33 | 100% | $0.00 | 3s |
| 2026-10-06 | groq/openai/gpt-oss-120b → groq/openai/gpt-oss-20b → gemini/gemini-3.8-flash (free tiers) | 2026.10.9 | bug | 1/7 | 14% | $0.00 | 1462s |

### Notes on the free-tier run (2026-10-06)

The 2026-10-06 row is **not a measure of model quality**. The free quotas ran out during the run:
- Groq gpt-oss-120b allows 200,000 tokens per day;
- Gemini 3.8 Flash allows 20 requests per day;
- one agent task needs roughly 100,000 tokens.

`fix-add`, `fix-slugify` and `fix-total` failed with `model error (rate_limit)` before any work. Earlier the same day, with quota left, Forge solved `bug-blank-lines` and twice the seeded bug in `examples/buggy` (tests green, `ok=true`). For a real comparison, run the suite with paid keys or a local model (Ollama).

