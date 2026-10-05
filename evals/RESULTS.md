# Eval results

One row per run of `forge eval` with `--report evals/RESULTS.md`: the model every role used,
the `PROMPTS_VERSION` of `src/forge/prompts.py`, the suite (`all`, or the `--suite` filter),
pass rate, cost and wall time. Live runs need API keys:

```bash
uv run forge eval --models anthropic/claude-sonnet,openai/gpt-5,gemini/gemini-2.5-pro,deepseek/deepseek-chat,groq/llama-3.3-70b-versatile --report evals/RESULTS.md
uv run forge eval --swebench swe-bench-lite.jsonl --limit 50 --report evals/RESULTS.md
```

Offline runs (`--fake`) replay each task's scripted solution; they check the harness, not a model.


| date | model | prompts | suite | passed | pass rate | cost | time |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-05 | fake/scripted | 2026.10.4 | all | 33/33 | 100% | $0.00 | 3s |
