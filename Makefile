.PHONY: lint type test ci install test-auth fmt fmtcheck

install:
	uv pip install -e ".[dev]"

lint:
	ruff check .

fmt:
	ruff format .

fmtcheck:
	ruff format --check .

type:
	mypy src/shared src/orchestrator

test:
	pytest -m "not integration and not external and not openapi" --cov=shared --cov=orchestrator --cov-report=term-missing -q

test-auth:
	pytest -m "not integration and not external" -m auth --cov=shared.auth --cov=orchestrator.auth_routes --cov=orchestrator.api_routes --cov-report=term-missing -v

ci: lint type test
