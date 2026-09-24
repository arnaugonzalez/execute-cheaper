# Plan: add a /health endpoint

Context: FastAPI app in `app/main.py`, tests in `tests/test_main.py` (pytest).

## Tasks
1. In `app/main.py`, add `GET /health` returning `{"status": "ok"}` with status 200.
2. In `tests/test_main.py`, add `test_health` using the existing `client` fixture.

## Out of scope
- Do not touch any other route, dependency or config file.

## Checks
- `pytest -q` passes.
- `ruff check app tests` passes.
