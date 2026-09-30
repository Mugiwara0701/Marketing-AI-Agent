.PHONY: install test lint up down health

install:
	pip install -e "libs/agentkit[dev]" ruff

test:
	pytest libs/agentkit
	for s in lead outreach content; do (cd services/$$s-service && python -m pytest tests -q) || exit 1; done

lint:
	ruff check .

up:
	docker compose up --build -d

down:
	docker compose down

health:
	for p in 8101 8102 8103; do curl -fsS localhost:$$p/health; echo; done
