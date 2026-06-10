.PHONY: lint type test ci install test-auth

install:
	uv pip install -e ".[dev]"

lint:
	ruff check .

type:
	mypy shared agent_1_adk

test:
	pytest -m "not integration and not external and not openapi" --cov=shared --cov=agent_1_adk --cov-report=term-missing -q

test-auth:
	pytest -m "not integration and not external" -m auth --cov=shared.auth --cov=agent_1_adk.auth_routes --cov=agent_1_adk.api_routes --cov-report=term-missing -v

ci: lint type test
