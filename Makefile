PYTHON ?= python3
JOBS ?= $(shell getconf _NPROCESSORS_ONLN)
OUTPUT ?= /tmp/gcc-builds-output
NETWORK ?= bridge
CONFIG ?= build.json
ACTIONLINT ?= actionlint
SHELLCHECK ?= shellcheck

.PHONY: build candidate verify discover sdk-lock check

build:
	$(PYTHON) scripts/build.py --jobs $(JOBS) --output "$(OUTPUT)" --network "$(NETWORK)" --config "$(CONFIG)"

candidate:
	$(PYTHON) scripts/build.py --jobs $(JOBS) --output "$(OUTPUT)" --network "$(NETWORK)" --config "$(CONFIG)" --defer-verification

verify:
	$(PYTHON) scripts/portability.py "$(ARCHIVE)" --output "$(OUTPUT)" --network "$(NETWORK)" --config "$(CONFIG)" $(if $(DISTRIBUTION),--distribution "$(DISTRIBUTION)")

discover:
	$(PYTHON) scripts/discover.py

sdk-lock:
	$(PYTHON) scripts/refresh_sdk.py --network $(NETWORK)

check:
	$(PYTHON) -m ruff check scripts tests
	$(PYTHON) -m ruff format --check scripts tests
	$(PYTHON) -m mypy scripts tests
	PYTHONPATH=scripts $(PYTHON) -m unittest discover -s tests -v
	for script in scripts/*.sh; do bash -n "$$script" || exit $$?; done
	$(SHELLCHECK) scripts/*.sh
	$(ACTIONLINT) .github/workflows/*.yml
