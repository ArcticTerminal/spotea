# Contributing

`main` is always meant to be stable and deployable. All changes go through
a branch and a pull request — no direct pushes to `main`.

## Branch naming

Branch off `main` and prefix the name with what kind of change it is:

- `feature/<short-description>` — new functionality
- `fix/<short-description>` — bug fix
- `hotfix/<short-description>` — urgent fix for something broken in a
  released/running instance

Example: `fix/avatar-cache-eviction`

## Workflow

1. `git checkout -b feature/your-change main`
2. Make your change, with tests where it makes sense.
3. Run the test suite and linter locally:
   ```bash
   pytest
   ruff check .
   ```
4. Push the branch and open a pull request against `main`.
5. CI (GitHub Actions) runs `pytest` and `ruff check .` on the PR — both
   must pass before merging.

## Local setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install yt-dlp bgutil-ytdlp-pot-provider
```

Or run the app itself via Docker as described in the README; the test
suite runs directly with `pytest` outside the container.
