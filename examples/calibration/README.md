# Methodology calibration — labeling kit

This directory holds the reference dataset and labeling instructions that produce the calibration evidence committed to in PRD §11.5. Until the dataset is populated and labels are collected, every published number is "panel-internal" — not "calibrated against human judgment."

This is **not yet populated.** It is the scaffold for the work to begin.

## What needs to happen, in order

1. **Build the reference dataset** (target: 100–200 scenarios)
   - Source from anonymized customer engagement scenarios + curated edge cases covering each failure-mode class in PRD §6.7
   - Land scenarios in `reference_dataset.jsonl` using the standard scenario shape
   - Span: happy path, schema-strict, tool-required, hallucination traps, multi-hop completeness, latency-strict, safety-adjacent, prompt-injection edge cases
2. **Recruit labelers** (target: ≥3 per scenario, drawn from senior delivery engineers)
   - Each labeler scores every scenario independently using `labels_template.csv`
   - No labeler should see another labeler's scores until all are submitted
3. **Compute inter-annotator agreement** per role
   - Krippendorff's α; flag roles with α < 0.6 for rubric refinement before the data is usable
4. **Run the panel** against the same scenarios with `mdk-eval run -c configs/calibration.yaml --runs 1`
5. **Compute calibration metrics** with `mdk-eval calibrate` (P3.1) — Cohen's κ, MAE, calibration plot, disagreement-mode breakdown
6. **Publish** the resulting `calibration_report.md` on the public methodology page

## Files (current and planned)

| File | Status | Purpose |
|---|---|---|
| `README.md` | present (this file) | Labeling protocol and instructions |
| `labels_template.csv` | present | Per-scenario per-labeler scoring template |
| `reference_dataset.jsonl` | **TODO** | The 100–200 reference scenarios |
| `labels/` | **TODO** | One CSV per labeler, named `labels/<labeler_id>.csv` |
| `panel_run/` | **TODO** | Output of `mdk-eval run` against the reference dataset |
| `calibration_report.md` | **TODO** | Computed metrics + interpretation; published on methodology page |

## Labeling instructions

For each scenario, score each role on a 0–100 scale using the same rubric the judges use (see `mdk_eval/evaluators/judges/prompts.py`). Independence is non-negotiable — discussing a score with another labeler before submission invalidates the agreement metric.

**Roles to label:** correctness, grounding, completeness, tool_usage, ux_tone, safety. (Workflow adherence, latency, and consistency are deterministic — not subject to human labeling.)

**For each score, also record:**
- The single most important reason for the score (one sentence)
- Any rubric ambiguity you hit (so we can refine prompts)
- Confidence (high / medium / low)

## Targets

These are the bars the calibration data must clear before the methodology is "calibrated":

| Metric | Target | Why |
|---|---|---|
| Inter-annotator α (per role) | ≥ 0.6 | Below this, the human "ground truth" is itself too noisy to compare against |
| Cohen's κ (panel vs. human consensus, per role) | ≥ 0.6 | Substantial agreement |
| Cohen's κ for Safety specifically | ≥ 0.75 | Excellent agreement; safety errors are highest-cost |
| MAE (panel vs. human, per role) | ≤ 10 (on 0–100 scale) | Score should be within one band of human judgment |

## Recalibration triggers

- **Mandatory:** every methodology version bump (per PRD §11.2)
- **Mandatory:** any judge provider model update (e.g. Anthropic ships claude-sonnet-4-7)
- **Routine:** quarterly, to catch silent provider drift

## Current status (2026-05-04)

Scaffold only. No reference dataset. No labels. No calibration report. This is the single largest evidence gap before GA.
