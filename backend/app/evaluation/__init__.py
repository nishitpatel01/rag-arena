"""Evaluation (LLM judge) and Battle Score computation for The RAG Arena v3."""

from __future__ import annotations

from app.evaluation.evaluator import evaluate_engine_result, run_evaluation
from app.evaluation.scoring import battle_score, diagnostics

__all__ = ["battle_score", "diagnostics", "evaluate_engine_result", "run_evaluation"]
