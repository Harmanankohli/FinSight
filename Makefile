.PHONY: lint type test ci install

install:
	uv pip install -e ".[dev]"

lint:
	ruff check .

type:
	mypy shared agent_1_adk

test:
	pytest -m "not integration and not external" --cov=shared --cov=agent_1_adk -q

ci: lint type test
