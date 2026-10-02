COMPOSE     := docker compose
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml

.PHONY: up up-dev down logs test migrate demo

up: ## Start the full stack (production-like)
	$(COMPOSE) up --build -d --wait

up-dev: ## Start the stack with source mounted and auto-reload
	$(COMPOSE_DEV) up --build -d --wait

down: ## Stop the stack
	$(COMPOSE_DEV) down

logs: ## Tail API and worker logs
	$(COMPOSE) logs -f api worker

test: ## Run the test suite in a container against the compose Postgres
	$(COMPOSE_DEV) up -d --wait postgres
	$(COMPOSE_DEV) run --rm --build --no-deps api pytest $(ARGS)

migrate: ## Apply database migrations
	$(COMPOSE) run --rm migrate

demo: ## Run the flagship scenario with curl against the running stack (browser: /demo)
	@sh docker/demo.sh
