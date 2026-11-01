.PHONY: help install lint typecheck test check demo showcase plan kind-demo docker media

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install the package with dev dependencies
	pip install -e ".[dev]"

lint: ## Run ruff
	ruff check src tests

typecheck: ## Run mypy (strict)
	mypy src

test: ## Run the test suite with coverage
	pytest

check: lint typecheck test ## Everything CI runs on Python

demo: ## Open the dashboard against a simulated cluster (http://127.0.0.1:8080)
	carbon-sim dashboard --port 8080

showcase: ## Offline backtest of every policy + HTML report in ./showcase
	carbon-sim showcase --output showcase

plan: ## Dry-run a 2h job with a 12h deadline against the live forecast
	carbon-sim plan --runtime 2h --within 12h --cpu 8

kind-demo: ## Build, deploy to a local kind cluster and port-forward the dashboard
	./scripts/kind-demo.sh

docker: ## Build the controller image
	docker build -t carbon-scheduler:dev .

media: ## Regenerate README screenshots, GIF and video (needs Playwright + ffmpeg)
	python docs/media/record.py
