.PHONY: install test lint format typecheck check up down health

install:
	pip install -e "libs/agentkit[dev]" ruff mypy types-PyYAML yamllint sqlfluff shellcheck-py actionlint-py pre-commit

test:
	pytest libs/agentkit
	for s in lead outreach content; do (cd services/$$s-service && python -m pytest tests -q) || exit 1; done

lint:
	ruff check .
	ruff format --check .
	yamllint --strict -c .yamllint.yaml .
	sqlfluff lint supabase/migrations
	shellcheck scheduler/scripts/*.sh

format:
	ruff format .
	ruff check --fix .

typecheck:
	mypy libs/agentkit/agentkit eval/runner
	for s in lead outreach content; do (cd services/$$s-service && mypy --config-file ../../mypy.ini app) || exit 1; done

# everything CI runs (deno, hadolint, actionlint and markdownlint run in CI only)
check: lint typecheck test

up:
	docker compose up --build -d

down:
	docker compose down

health:
	for p in 8101 8102 8103; do curl -fsS localhost:$$p/health; echo; done
