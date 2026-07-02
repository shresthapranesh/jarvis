# jarvis — Docker deploy & update targets. See docs/DEPLOY.md for the threat
# model and configuration notes. Data lives in the `jarvis-data` volume; every
# target here is safe for it (nothing runs `down -v` or prunes volumes).

COMPOSE    := docker compose
SERVICE    := jarvis
BACKUP_DIR := backups

.DEFAULT_GOAL := help

.PHONY: help deploy update backup logs status shell down restart prune

help: ## List available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

deploy: .env ## First-time deploy: build image, create volume, start on 127.0.0.1:8000
	$(COMPOSE) up -d --build
	@echo "==> started — http://127.0.0.1:8000  (make logs to follow startup)"

update: .env backup ## Update to current checkout: snapshot volume, rebuild, recreate container
	$(COMPOSE) up -d --build
	@echo "==> updated — make logs to confirm it comes up healthy"

backup: ## Snapshot the jarvis-data volume to backups/jarvis-data-<date>.tgz
	@mkdir -p $(BACKUP_DIR)
	docker run --rm -v jarvis-data:/data -v "$$PWD/$(BACKUP_DIR):/backup" alpine \
		tar czf /backup/jarvis-data-$$(date +%F-%H%M%S).tgz -C /data .
	@ls -lh $(BACKUP_DIR) | tail -1

logs: ## Follow container logs
	$(COMPOSE) logs -f $(SERVICE)

status: ## Container status + health
	$(COMPOSE) ps $(SERVICE)

shell: ## Shell inside the running container (config/model CLI lives here)
	$(COMPOSE) exec $(SERVICE) bash

down: ## Stop and remove the container (volume is kept)
	$(COMPOSE) down

restart: ## Restart the container without rebuilding
	$(COMPOSE) restart $(SERVICE)

prune: ## Remove dangling images left by rebuilds (never touches volumes)
	docker image prune -f

.env:
	@echo "error: .env missing — run: cp .env.example .env  and fill in GOOGLE_API_KEY etc." >&2
	@exit 1
