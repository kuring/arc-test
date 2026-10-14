PYTHON ?= python3
SCENARIO ?= all
COUNT ?= 3
DURATION ?= 10
PATH := $(CURDIR)/.bin:$(PATH)
export PATH

.PHONY: help tools doctor up smoke test drain resume submit status collect cancel restart down check
help:
	@$(PYTHON) scripts/arc_test.py --help
tools:
	@bash scripts/tools.sh
doctor up smoke drain resume status collect cancel restart down:
	@$(PYTHON) scripts/arc_test.py $@
test:
	@$(PYTHON) scripts/arc_test.py test --scenario $(SCENARIO)
submit:
	@$(PYTHON) scripts/arc_test.py submit --count $(COUNT) --seconds $(DURATION)
check:
	@$(PYTHON) -m unittest discover -s tests -v
	@$(PYTHON) -m py_compile scripts/arc_test.py
