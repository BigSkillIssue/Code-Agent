# buggy

A tiny project with one failing test (`add` subtracts). Forge's Phase 0 gate fixes it:

```bash
cd examples/buggy
python -m pytest -q          # 1 failed
forge --fake ../../tests/fixtures/fake/fix_buggy.json "Make the tests pass"
python -m pytest -q          # 2 passed
```
