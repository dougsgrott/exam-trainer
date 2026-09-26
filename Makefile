# One place that knows the order of the pipeline.
#
# The tools stay PEP 723 standalone scripts -- `uv run tools/X.py` resolves their
# own inline dependencies and does not use this project's environment. Only the
# `examkb` targets need `uv sync`.

UV := uv
KB := kb

PARSE  := $(UV) run tools/parse_udemy.py --check
SHARDS := $(UV) run tools/build_shards.py
BUILD  := $(UV) run tools/build_kb.py
VERIFY := $(UV) run tools/verify_lossless.py
STATS  := $(UV) run tools/kb_stats.py
REPORT := $(UV) run tools/build_report.py

.DEFAULT_GOAL := help
.PHONY: help sync test test-slow test-all pipeline parse shards build verify stats report kb-hash db-upgrade backup ingest serve doctor clean-kb

help:  ## Show this help
	@grep -hE '^[a-z][a-zA-Z0-9_-]*:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'

sync:  ## Install the examkb package and its pinned dependencies
	$(UV) sync

test:  ## Run the fast tests (the slow full-corpus runs are deselected)
	$(UV) run pytest

test-slow:  ## Run only the slow full-corpus tests
	$(UV) run pytest -m slow

test-all:  ## Run every test, fast and slow
	$(UV) run pytest -m ""

# ----------------------------------------------------------------- kb/ pipeline

pipeline:  ## Run the full documented pipeline, in order
	$(PARSE)
	$(SHARDS)
	$(BUILD)
	$(VERIFY)
	$(STATS)
	$(REPORT)

parse:  ## data/ -> kb/ canonical JSON + JSONL, and validate
	$(PARSE)

shards:  ## Index the question shards into kb/shards.json (no parser writes it)
	$(SHARDS)

build:  ## kb/ -> kb/study/**.md
	$(BUILD)

verify:  ## Prove kb/ lost nothing against data/
	$(VERIFY)

stats:  ## kb/ -> kb/stats.json + terminal summary
	$(STATS)

report:  ## stats -> kb/reports/*.html
	$(REPORT)

kb-hash:  ## Print the sha256 of every file under kb/ (the purity baseline)
	@find $(KB) -type f -exec sha256sum {} + | sort

clean-kb:  ## Delete kb/; it is a pure function of data/ and rebuilds
	rm -rf $(KB)

# --------------------------------------------------------------------- examkb
#
# `restore` is deliberately absent: it overwrites the journal, and a destructive
# operation should not be one keystroke away from `make report`.

db-upgrade:  ## Apply database migrations (takes a verified backup first)
	$(UV) run examkb db upgrade

backup:  ## Snapshot the journal database off-box and verify the snapshot
	$(UV) run examkb backup

ingest:  ## kb/ -> SQLite projection
	$(UV) run examkb ingest

serve:  ## Run the web app on 127.0.0.1
	$(UV) run examkb serve

doctor:  ## Check that this box is set up correctly
	$(UV) run examkb doctor
