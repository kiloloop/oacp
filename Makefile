# OACP — task runner
# Usage: make <target> [PROJECT=<name>] [ARGS="--flag ..."]

SCRIPTS_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))scripts
PROJECT     ?=
ARGS        ?=

.DEFAULT_GOAL := help

# ── Internal helpers ─────────────────────────────────────────────────────

_require-project:
ifndef PROJECT
	$(error PROJECT is required. Usage: make <target> PROJECT=<name>)
endif

# ── Targets ──────────────────────────────────────────────────────────────

.PHONY: help update test docs validate validate-msg validate-card send quality packet preflight doctor

help: ## Show available targets (default)
	@echo "OACP — task runner"
	@echo ""
	@echo "Usage: make <target> [PROJECT=<name>] [ARGS=\"...\"]"
	@echo ""
	@grep -E '^[a-z][-a-z]+:.*## ' $(MAKEFILE_LIST) | \
		awk -F ':.*## ' '{ printf "  %-14s %s\n", $$1, $$2 }'

update: _require-project ## Sync existing workspace with latest structure
	bash $(SCRIPTS_DIR)/update_workspace.sh $(PROJECT) $(ARGS)

test: ## Run all tests
	python3 -m pytest $(SCRIPTS_DIR)/../tests -v $(ARGS)

docs: ## Regenerate the README command table from oacp --help
	python3 $(SCRIPTS_DIR)/gen_readme_commands.py --write

validate-msg: ## Validate inbox messages
	python3 $(SCRIPTS_DIR)/validate_message.py $(ARGS)

validate-card: ## Validate agent card YAML files
	python3 $(SCRIPTS_DIR)/validate_agent_card.py $(ARGS)

validate: validate-msg ## Alias for validate-msg (backward compat)

preflight: ## Run unified quality checks (use ARGS="--full" to include tests)
	python3 $(SCRIPTS_DIR)/preflight.py $(ARGS)

doctor: ## Check environment and workspace health
	python3 $(SCRIPTS_DIR)/oacp_doctor.py $(ARGS)

send: _require-project ## Send an inbox message
	python3 $(SCRIPTS_DIR)/send_inbox_message.py $(PROJECT) $(ARGS)

quality: ## Run quality gate check
	python3 $(SCRIPTS_DIR)/check_quality_gate.py $(ARGS)

packet: _require-project ## Create a review/findings/merge packet
	bash $(SCRIPTS_DIR)/init_packet.sh $(PROJECT) $(ARGS)
