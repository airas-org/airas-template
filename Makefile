# Evaluation entry points (managed by AIRAS — not part of the agent's allowed files).
#
# Metrics are computed by airas-eval, never by experiment code. The experiment
# writes raw evaluation inputs; this Makefile runs the pinned airas-eval CLI on
# them. Task types come from the research plan (.research/evaluation.json);
# workflows may override via AIRAS_EVAL_TASKS. Scores seen here are for the
# agent's own iteration — the official numbers are recomputed by AIRAS from the
# same input files in an environment the agent cannot edit.

RESULTS_DIR      ?= .research/results
EVAL_PLAN        ?= .research/evaluation.json
AIRAS_EVAL_TASKS ?= $(shell python3 -c 'import json,sys; d=json.load(open("$(EVAL_PLAN)")); print(" ".join(d.get("task_types", [])))')
AIRAS_EVAL        = uv run --group eval airas-eval

.PHONY: evaluate validate-inputs schema list-tasks

## Score every task type in the plan for one run: make evaluate RUN_ID=<run_id>
evaluate: _require_run_id _require_tasks
	@mkdir -p "$(RESULTS_DIR)/$(RUN_ID)/evaluation"
	@for t in $(AIRAS_EVAL_TASKS); do \
		echo "=== [AIRAS-EVAL] $$t for $(RUN_ID)"; \
		$(AIRAS_EVAL) score $$t \
			--inputs "$(RESULTS_DIR)/$(RUN_ID)/eval_inputs/$$t.json" \
			--output "$(RESULTS_DIR)/$(RUN_ID)/evaluation/$$t.json" || exit 1; \
	done

## Check the input files against the contract without scoring
validate-inputs: _require_run_id _require_tasks
	@for t in $(AIRAS_EVAL_TASKS); do \
		$(AIRAS_EVAL) validate $$t --inputs "$(RESULTS_DIR)/$(RUN_ID)/eval_inputs/$$t.json" || exit 1; \
	done

## Print the JSON Schema of the input file(s) the experiment must produce
schema: _require_tasks
	@for t in $(AIRAS_EVAL_TASKS); do $(AIRAS_EVAL) schema $$t; done

## Print what each planned task type returns
list-tasks: _require_tasks
	@for t in $(AIRAS_EVAL_TASKS); do $(AIRAS_EVAL) list $$t; done

_require_run_id:
	@test -n "$(RUN_ID)" || { echo "RUN_ID is required, e.g. make evaluate RUN_ID=proposed-resnet-cifar10"; exit 1; }

_require_tasks:
	@test -n "$(AIRAS_EVAL_TASKS)" || { echo "no task types: $(EVAL_PLAN) has no task_types and AIRAS_EVAL_TASKS is unset"; exit 1; }
