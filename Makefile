.PHONY: clean test lint format-check check reset-dev-db

PYTHON ?= uv run python
PYTEST ?= uv run pytest
DEV_DB_PATH ?= /tmp/telegram-bot-dev.db

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
	rm -rf .pytest_cache .coverage htmlcov build dist *.egg-info

test:
	$(PYTEST) -q

lint:
	$(PYTHON) -m compileall -q src tests

format-check:
	git diff --check

check: lint format-check test

reset-dev-db:
	@test "$(CONFIRM_RESET_DEV_DB)" = "YES" || (echo "Refusing reset: set CONFIRM_RESET_DEV_DB=YES" >&2; exit 1)
	@test "$(DEV_DB_PATH)" != "" || (echo "Refusing reset: DEV_DB_PATH is empty" >&2; exit 1)
	@test "$(DEV_DB_PATH)" != "data/bot.db" && "$(DEV_DB_PATH)" != "$(PWD)/data/bot.db" || (echo "Refusing reset of production database path" >&2; exit 1)
	@if test -f "$(DEV_DB_PATH)"; then cp -p "$(DEV_DB_PATH)" "$(DEV_DB_PATH).backup.$$(date +%Y%m%d%H%M%S)"; fi
	rm -f "$(DEV_DB_PATH)" "$(DEV_DB_PATH)-wal" "$(DEV_DB_PATH)-shm" "$(DEV_DB_PATH)-journal"
	@echo "Development database reset: $(DEV_DB_PATH)"
