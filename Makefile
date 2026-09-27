PYTHON ?= python
COMPOSE = "$(PYTHON)" scripts/compose.py

.PHONY: up down migrate seed test check fmt build bootstrap voices backup demo load verify-backup

bootstrap:
	"$(PYTHON)" scripts/bootstrap.py

build: bootstrap
	$(COMPOSE) build

up: bootstrap
	$(COMPOSE) up --build --detach --wait --wait-timeout 240

down:
	$(COMPOSE) down

migrate: bootstrap
	$(COMPOSE) run --rm migrate

seed: bootstrap
	$(COMPOSE) run --rm backend python -m app.seeds.import_classifier
	$(COMPOSE) run --rm backend python -m app.seeds.import_streets
	$(COMPOSE) run --rm backend python -m app.seeds.create_demo_users
	$(COMPOSE) run --rm backend python -m app.seeds.manual_scenarios
	$(COMPOSE) run --rm backend python -m app.seeds.directory

voices:
	"$(PYTHON)" scripts/build_voices.py

models:
	"$(PYTHON)" scripts/prepare_models.py

technical-status:
	"$(PYTHON)" scripts/technical_operations.py collect

monitor:
	"$(PYTHON)" scripts/technical_operations.py watch

backup: bootstrap
	$(COMPOSE) run --rm backup /scripts/backup.sh

demo: models up migrate seed
	$(COMPOSE) run --rm backend python -m app.seeds.demo

load:
	"$(PYTHON)" scripts/prepare_acceptance.py load
	"$(PYTHON)" scripts/check_load.py --think-time 2

verify-backup:
	"$(PYTHON)" scripts/verify_backup.py

test: bootstrap
	$(COMPOSE) up --detach --wait --no-build languagetool
	$(COMPOSE) run --rm checks pytest
	$(COMPOSE) run --rm --no-deps checks pytest -q -o "pythonpath=/app /installer /installer/scripts" -o cache_dir=/tmp/installer-pytest /installer/scripts
	$(COMPOSE) run --rm --no-deps frontend_checks npm run test

check: bootstrap
	$(COMPOSE) config --quiet
	$(COMPOSE) up --detach --wait --no-build languagetool
	$(COMPOSE) run --rm checks ruff check .
	$(COMPOSE) run --rm checks ruff format --check .
	$(COMPOSE) run --rm checks mypy --strict app tests alembic
	$(COMPOSE) run --rm checks pytest
	$(COMPOSE) run --rm --no-deps checks pytest -q -o "pythonpath=/app /installer /installer/scripts" -o cache_dir=/tmp/installer-pytest /installer/scripts
	$(COMPOSE) run --rm --no-deps frontend_checks npm run check

fmt: bootstrap
	$(COMPOSE) run --rm checks ruff check --fix .
	$(COMPOSE) run --rm checks ruff format .
	$(COMPOSE) run --rm --no-deps frontend_checks npm run fmt
