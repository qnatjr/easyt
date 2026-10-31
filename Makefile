# YouTube Creation Dashboard — dev tasks
#
# Override anything on the command line:
#   make run PORT=9000
#   make test ARGS="-k scenes -v"

PYTHON  ?= python3
VENV    ?= .venv
BIN     := $(VENV)/bin
PY      := $(BIN)/python
PIP     := $(BIN)/pip
UVICORN := $(BIN)/uvicorn

APP     ?= app.main:app
HOST    ?= 127.0.0.1
PORT    ?= 8000
ARGS    ?=

.DEFAULT_GOAL := help
.PHONY: help venv install env setup run serve test lint-imports routes open stop clean distclean

help: ## Show this help
	@echo "YouTube Creation Dashboard"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Vars: HOST=$(HOST) PORT=$(PORT) VENV=$(VENV)"

$(PY):
	$(PYTHON) -m venv $(VENV)

venv: $(PY) ## Create the virtualenv

install: venv ## Install dependencies into the virtualenv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

env: ## Create .env from .env.example if it does not exist
	@if [ -f .env ]; then \
		echo ".env already exists — leaving it alone"; \
	else \
		cp .env.example .env; \
		echo ".env created — put your MIMO_API_KEY in it"; \
	fi

setup: install env ## Full first-time setup (venv + deps + .env)
	@echo "Setup done. Run 'make run'."

run: venv ## Run the dev server with auto-reload
	@if [ ! -f .env ]; then echo "No .env — run 'make env' and set MIMO_API_KEY"; exit 1; fi
	$(UVICORN) $(APP) --reload --host $(HOST) --port $(PORT)

serve: venv ## Run the server without reload
	$(UVICORN) $(APP) --host $(HOST) --port $(PORT)

test: venv ## Run the test suite (offline, MIMO mocked)
	$(PY) -m pytest $(ARGS)

routes: venv ## List every HTTP route the app exposes
	@$(PY) -c "from app.main import app; s = app.openapi(); [print('%-7s %s' % (m.upper(), p)) for p, ops in sorted(s['paths'].items()) for m in sorted(ops)]"

open: ## Open the dashboard in a browser
	@open http://$(HOST):$(PORT) 2>/dev/null || echo "Open http://$(HOST):$(PORT)"

stop: ## Stop whatever is listening on the configured port
	@pids=$$(lsof -ti tcp:$(PORT) | tr '\n' ' '); \
	if [ -n "$$pids" ]; then kill $$pids && echo "Stopped pid(s) $$pids on port $(PORT)"; \
	else echo "Nothing listening on port $(PORT)"; fi

clean: ## Remove caches and build junk (keeps .venv and data/)
	find . -path ./$(VENV) -prune -o -name '__pycache__' -type d -print0 | xargs -0 rm -rf
	rm -rf .pytest_cache

distclean: clean ## Also remove the virtualenv (data/ and .env are never touched)
	rm -rf $(VENV)
