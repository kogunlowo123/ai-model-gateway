# Thin wrapper around tasks.py so `make` and `python tasks.py` never diverge.
# tasks.py is the source of truth and is the supported entry point on Windows.
#
# Every target below is generated from the same table; if you add a task there,
# add its name to TASKS here and nothing else changes.

PY ?= python

TASKS := setup fmt lint typecheck test test-unit test-integration test-security \
         test-e2e test-meta workloads workloads-check fit calibrate evaluate \
         baseline resilience models doctor check-gateway examples site security \
         docker-build smoke all

.DEFAULT_GOAL := help
.PHONY: help $(TASKS)

help:
	@$(PY) tasks.py --list

$(TASKS):
	@$(PY) tasks.py $@
