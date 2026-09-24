# Contributing

Small project, small rules:

- One file (`execute_cheaper.py`), standard library only. A new dependency needs a strong reason.
- Tests use fake `claude`/`agy` binaries and never touch the network or spend quota:
  `python -m unittest -v tests.test_execute_cheaper`. Add a fake-binary test for every behaviour change.
- `ruff check .` must pass.
- New provider presets are welcome in `EXAMPLE_CONFIG` (and `config.example.json`, a test keeps
  them in sync) with the price date and a link to the provider's pricing page in the PR.
- Open an issue before large changes (new adapters, new commands).
