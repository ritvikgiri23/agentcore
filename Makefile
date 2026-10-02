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
	$(COMPOSE_DEV) run --rm --no-deps api pytest $(ARGS)

migrate: ## Apply database migrations
	$(COMPOSE) run --rm migrate

demo: ## Placeholder: the keyless browser demo arrives in a later ticket
	@echo "Demo not implemented yet. Start the stack with 'make up', then: curl localhost:$${API_PORT:-8000}/api/v1/health"
