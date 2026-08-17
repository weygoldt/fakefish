# fakefish — convenience targets. The real gate is check.sh; this is just a front door.
.PHONY: check gen sync test lint clean help

help:
	@echo "make check   full acceptance gate (codegen + sync + host tests + Teensy compile + python)"
	@echo "make gen     regenerate stim_levels.h + _constants.py from shared/stim_constants.json"
	@echo "make sync    copy firmware/eel_core into each sketch's src/eel_core"
	@echo "make test    python tests only"
	@echo "make lint    ruff only"
	@echo
	@echo "Flashing a Teensy and scoping the output is a bench step and is NOT part of 'check'."

check:
	@bash check.sh

# Edit shared/stim_constants.json, then run this, then commit both generated files.
gen:
	@uv run fakefish-gen-constants

# Edit firmware/eel_core/, then run this, then commit each sketch's src/eel_core.
sync:
	@bash firmware/sync_core.sh

test:
	@uv run pytest -q

lint:
	@uv run ruff check .

clean:
	@rm -rf figs/ .pytest_cache .ruff_cache
