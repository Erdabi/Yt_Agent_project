# `-include` (not `include`): don't fail if .env doesn't exist yet, e.g.
# right after a fresh clone, before `cp .env.example .env`.
-include .env

.PHONY: up down restart logs ps build migrate migrate-revision psql

up: ## Build images if needed and start the full stack in the background.
	docker compose up -d --build

down: ## Stop and remove all containers (data volumes are kept).
	docker compose down

restart: ## Restart every running service in place.
	docker compose restart

logs: ## Tail logs from every service.
	docker compose logs -f --tail=200

ps: ## Show the status of every service.
	docker compose ps

build: ## Rebuild every service's image without starting anything.
	docker compose build

migrate: ## Run `alembic upgrade head` against the running Postgres.
	docker compose run --rm migrate

migrate-revision: ## Autogenerate a new migration: `make migrate-revision m="add x"`.
	docker compose run --rm migrate alembic revision --autogenerate -m "$(m)"

psql: ## Open a psql shell into the running Postgres container.
	docker compose exec postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB)
