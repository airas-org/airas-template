# Run and evaluation entry points (managed by AIRAS — not part of the agent's allowed files).
#
# `make run RUN_ID=<run_id> MODE=<sanity|pilot|full>` is the one entry point the
# workflows and airas call. What it runs is the run's kind, read from
# config/run/<run_id>.yaml:
#
#   (no kind)    an experiment: src.main, then airas-eval on its eval_inputs,
#                then src.evaluate for metrics.json and the figures
#   kind: lean   a proof: `lake build <module>` in lean/, then `lake exe
#                airas-report` writes .research/results/<run_id>/lean.json for
#                <decl>. The yaml also names them, e.g.
#                    kind: lean
#                    module: Airas.Thm1
#                    decl: thm1
#                MODE is sanity (the statement type-checks, sorry allowed) or
#                full (a sorry-free proof); Lean has no pilot stage.
#
# Metrics are computed by airas-eval, never by experiment code. The experiment
# writes raw evaluation inputs; this Makefile runs the pinned airas-eval CLI on
# them. Task types come from the research plan (.research/evaluation.json);
# workflows may override via AIRAS_EVAL_TASKS. Scores seen here are for the
# agent's own iteration — the official numbers are recomputed by AIRAS from the
# same input files in an environment the agent cannot edit. Likewise lean.json
# is written by airas-report, never by the proof's author, for the record gate
# to re-derive.

RESULTS_DIR      ?= .research/results
MODE             ?= full
EVAL_PLAN        ?= .research/evaluation.json
RUN_CONFIG        = config/run/$(RUN_ID).yaml
LEAN_DIR         ?= lean
AIRAS_EVAL_TASKS ?= $(shell python3 -c 'import json,sys; d=json.load(open("$(EVAL_PLAN)")); print(" ".join(d.get("task_types", [])))')
AIRAS_EVAL        = uv run --group eval airas-eval

# `key: value` from the run config, with quotes and a trailing comment stripped.
run_config_value  = $(shell sed -n 's/^$(1):[[:space:]]*//p' "$(RUN_CONFIG)" 2>/dev/null \
                      | sed -e 's/[[:space:]]*\#.*$$//' -e 's/^"\(.*\)"$$/\1/' -e "s/^'\(.*\)'$$/\1/" | head -1)
RUN_KIND          = $(call run_config_value,kind)
LEAN_MODULE       = $(call run_config_value,module)
LEAN_DECL         = $(call run_config_value,decl)
LEAN_RUN_DIR      = $(abspath $(RESULTS_DIR))/$(RUN_ID)

.PHONY: run run-experiment run-lean evaluate validate-inputs schema list-tasks

## Run one run_id at one stage: make run RUN_ID=<run_id> MODE=<sanity|pilot|full>
run: _require_run_id
	@case "$(RUN_KIND)" in \
	  ""|experiment) $(MAKE) run-experiment RUN_ID="$(RUN_ID)" MODE="$(MODE)" ;; \
	  lean)          $(MAKE) run-lean       RUN_ID="$(RUN_ID)" MODE="$(MODE)" ;; \
	  *) echo "unknown kind '$(RUN_KIND)' in $(RUN_CONFIG): expected no kind (an experiment) or 'lean'"; exit 1 ;; \
	esac

## The experiment chain. A run that stops after src.main leaves no metrics.json
## for the record gate to compare, so the three steps are one target.
run-experiment: _require_run_id
	uv run python -u -m src.main run=$(RUN_ID) results_dir="$(RESULTS_DIR)" mode=$(MODE)
	$(MAKE) evaluate RUN_ID="$(RUN_ID)"
	uv run python -u -m src.evaluate results_dir="$(RESULTS_DIR)" run_ids="[\"$(RUN_ID)\"]"

## A proof. The build log is kept next to lean.json; airas-report reads it, so a
## failed build is reported rather than hidden, and fails the run itself.
run-lean: _require_run_id _require_lean_run
	@mkdir -p "$(LEAN_RUN_DIR)"
	cd "$(LEAN_DIR)" && lake exe cache get
	cd "$(LEAN_DIR)" && lake build "$(LEAN_MODULE)" 2>&1 | tee "$(LEAN_RUN_DIR)/build.txt"; \
	  lake exe airas-report --module "$(LEAN_MODULE)" --decl "$(LEAN_DECL)" --mode "$(MODE)" \
	    --build-log "$(LEAN_RUN_DIR)/build.txt" --out "$(LEAN_RUN_DIR)/lean.json"
	@case "$(MODE)" in sanity) echo "SANITY_VALIDATION: PASS" ;; esac

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

_require_lean_run:
	@test -n "$(LEAN_MODULE)" && test -n "$(LEAN_DECL)" || { echo "$(RUN_CONFIG) must name 'module' and 'decl' for a lean run"; exit 1; }
	@test "$(MODE)" = sanity || test "$(MODE)" = full || { echo "Lean runs have no '$(MODE)' stage: use sanity (the statement type-checks, sorry allowed) or full (a sorry-free proof)"; exit 1; }
