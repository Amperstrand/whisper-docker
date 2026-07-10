.DEFAULT_GOAL := help

ifneq (,$(wildcard ./.env))
    include .env
    export
endif

.PHONY: help build prefetch run batch test test-full clean prune gpu-status

DOCKER_COMPOSE := docker compose
WHISPER_MODEL ?= turbo
ANALYSIS ?= diarize,vad,emotion,classify,summarize

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

build: ## Build the Docker image
	$(DOCKER_COMPOSE) build

prefetch: build ## Pre-download all models (transcription + analysis + HF summarizer + Ollama LLM)
	@echo "=== Pre-fetching all models ==="
	$(DOCKER_COMPOSE) run --rm \
		-e HF_TOKEN=$${HF_TOKEN:-} \
		transcribe python3 /app/transcribe.py --prefetch
	@echo ""
	@echo "=== Pulling Ollama LLM model ==="
	$(DOCKER_COMPOSE) --profile summarize up -d ollama
	@echo "Waiting for Ollama to be ready..."
	@for i in $$(seq 1 30); do \
		if docker exec whisper-ollama ollama list > /dev/null 2>&1; then \
			echo "Ollama ready."; break; \
		fi; \
		echo "  waiting... ($$i/30)"; \
		sleep 2; \
	done
	docker exec whisper-ollama ollama pull llama3.1:8b
	docker exec whisper-ollama ollama pull gemma3:4b || true
	$(DOCKER_COMPOSE) --profile summarize stop ollama
	@echo ""
	@echo "=== All models cached in ~/.cache/whisper-docker/ ==="

run: build ## Transcribe a single file (FILE=path/to/audio.mp3)
	@test -n "$(FILE)" || (echo "Usage: make run FILE=path/to/audio.mp3" && exit 1)
	@echo "Transcribing $(FILE)..."
	cp "$(FILE)" input/
	$(DOCKER_COMPOSE) up --build --abort-on-container-exit
	@echo "Results in output/"

batch: build ## Batch transcribe a folder recursively (DIR=path/to/folder)
	@test -n "$(DIR)" || (echo "Usage: make batch DIR=path/to/folder" && exit 1)
	python3 batch.py "$(DIR)"

test: ## Run the smoke test
	./test.sh

test-full: build ## Run comprehensive test (all analysis stages, summarization, output formats)
	./test.sh --full

clean: ## Stop containers, remove images, clean temp files (keeps cached models)
	@echo "=== Cleaning Docker artifacts ==="
	$(DOCKER_COMPOSE) --profile summarize down --remove-orphans 2>/dev/null || true
	$(DOCKER_COMPOSE) down --remove-orphans 2>/dev/null || true
	docker image rm whisper-docker-transcribe 2>/dev/null || true
	docker container prune -f 2>/dev/null || true
	docker image prune -f 2>/dev/null || true
	rm -rf .batch-input .batch-output
	@echo ""
	@echo "Docker images and containers cleaned."
	@echo "Model caches preserved at ~/.cache/whisper-docker/"
	@echo "Rebuild with:  make build"
	@echo "Delete cached models with:  make purge"

purge: clean ## Delete everything including cached models
	rm -rf ~/.cache/whisper-docker/
	@echo "All caches purged. Next run will re-download models."

prune: ## Aggressively reclaim Docker disk space (removes unused images, build cache)
	@echo "=== Aggressive Docker cleanup ==="
	$(DOCKER_COMPOSE) --profile summarize down --remove-orphans 2>/dev/null || true
	$(DOCKER_COMPOSE) down --remove-orphans 2>/dev/null || true
	docker system prune -af --volumes 2>/dev/null || true
	@echo ""
	@echo "NOTE: Ollama model cache at ~/.cache/whisper-docker/ollama/ was preserved."
	@echo "NOTE: HuggingFace model caches at ~/.cache/whisper-docker/ were preserved."

gpu-status: ## Show GPU status and lock info
	@nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader 2>/dev/null || echo "GPU not found"
	@if [ -f /tmp/whisper-gpu.lock ]; then \
		echo "GPU lock: HELD (pid $$(fuser /tmp/whisper-gpu.lock 2>/dev/null | tr -d ' '))"; \
	else \
		echo "GPU lock: free"; \
	fi
