.PHONY: llm-verify install hooks test lint format typecheck check run demo

install:
	pip install -r requirements-dev.txt yamllint sqlfluff shellcheck-py actionlint-py pre-commit

# Install the git pre-commit hook so every commit lints the staged files
hooks:
	pre-commit install --install-hooks

test:
	pytest libs/agentkit agent sandbox api

lint:
	ruff check .
	ruff format --check .
	yamllint --strict -c .yamllint.yaml .
	sqlfluff lint supabase/migrations
	shellcheck deploy/*.sh

format:
	ruff format .
	ruff check --fix .

typecheck:
	mypy libs/agentkit/agentkit eval/runner agent api

# everything CI runs (deno, hadolint, actionlint and markdownlint run in CI only)
check: lint typecheck test

# Check setup, run the whole pipeline once for real, print the results
demo:
	python -m agent demo

# One bounded daily run (send approved emails -> leads -> blog). Normally started by the systemd timer.
run:
	python -m agent run

# Verify a running LLM host: LLM_BASE_URL, LLM_API_KEY (and EMBED_BASE_URL) must be set
llm-verify:
	PYTHON=.venv/bin/python bash services/llm-service/scripts/verify.sh
