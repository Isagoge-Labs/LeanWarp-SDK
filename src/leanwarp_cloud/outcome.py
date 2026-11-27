"""Separate transport completion from Lean's mathematical result.

Every verification result (a check, each batch entry, a fixed-target verification)
has one status. Verdicts: ``ok``, ``error`` (Lean's errors) and ``rejected``
(refused; ``failure.kind`` says why). No verdict: ``timeout``,
``infrastructure_failure`` and ``not_checked``; the proof was neither accepted
nor refuted, so retry it (``failure.retryable``) rather than repair it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# How far a suggestion's recheck reached: the whole file is proved (strict), or
# its declaration is, while others still have holes (declaration).
SUGGESTION_VERIFICATIONS = frozenset({"strict", "declaration"})
#: The statuses that decide nothing about the proof.
NON_VERDICTS = frozenset({"timeout", "infrastructure_failure", "not_checked"})
#: The receipt policy of a fixed-target verification the checker accepted.
FIXED_TARGET_POLICY = "fixed_target_kernel_check_v2"
_BATCHES = frozenset({"check_files", "verify_batch"})
#: A batch's counters and the entry statuses each counts; together they partition the batch.
BATCH_COUNTERS: Mapping[str, frozenset[str]] = {
    "passed": frozenset({"ok"}),
    "failed": frozenset({"error", "rejected"}),
    "timed_out": frozenset({"timeout"}),
    "infrastructure_failures": frozenset({"infrastructure_failure"}),
    "not_checked": frozenset({"not_checked"}),
}
#: The list a listing tool returns, even for a file that does not compile.
_LISTINGS = {"holes": "holes", "extract_decls": "declarations", "proof_states": "rows"}
#: The `inspect` statuses that promise goals.
_INSPECTED = frozenset({"available", "proof_state", "metadata_only"})
# The statuses each source tool documents as its answer. Anything else, a
# missing status included, is no answer at all.
_ANSWERS: dict[str, frozenset[str]] = {
    "check": frozenset({"ok", "error", "rejected"}),
    "verify_target": frozenset({"ok", "error", "rejected"}),
    "inspect": frozenset({"available", "proof_state", "metadata_only", "unavailable", "error"}),
    "suggest": frozenset({"closed", "not_closed", "error"}),
    "holes": frozenset({"ok", "error"}),
    "extract_decls": frozenset({"ok", "error"}),
    # `unsupported`: the environment's build cannot list them; no answer.
    "proof_states": frozenset({"ok", "error"}),
    "sorry2lemma": frozenset(
        {"extracted", "partial", "unchanged", "no_targets", "failed", "error"}
    ),
    # A proof rewrite is `rejected` when LeanWarp's audit refused the rewritten file.
    "have2lemma": frozenset(
        {"extracted", "partial", "unchanged", "no_targets", "failed", "rejected", "error"}
    ),
    "simplify_proofs": frozenset(
        {"simplified", "partial", "unchanged", "no_targets", "failed", "rejected", "error"}
    ),
    "repair_proofs": frozenset(
        {"repaired", "partial", "no_targets", "failed", "rejected", "error"}
    ),
}
# Each file rewrite's statuses that answered without a failure.
REWRITE_SUCCESSES: Mapping[str, frozenset[str]] = {
    "sorry2lemma": frozenset({"extracted", "partial", "unchanged", "no_targets"}),
    "have2lemma": frozenset({"extracted", "partial", "unchanged", "no_targets"}),
    "simplify_proofs": frozenset({"simplified", "partial", "unchanged", "no_targets"}),
    "repair_proofs": frozenset({"repaired", "partial", "no_targets"}),
}
# Error codes of a source tool's dependency preparation that stopped without
# Lean's answer: no answer either.
_STOPPED = frozenset({"timeout", "resource_exhausted", "cancelled"})


@dataclass(frozen=True)
class Judgement:
    """What an operation established: ``success`` and ``decided`` are None while it runs.

    ``decided`` is true only for an answer recognised positively: a verdict (or
    a tool's documented answer) with every field its status promises, of the
    right type. A missing, unknown or malformed result, a non-verdict, and an
    acceptance without its evidence are not decided.
    """

    success: bool | None
    decided: bool | None

    @property
    def exit_code(self) -> int:
        """0 success, 1 a decided failure, 3 running, 4 no verdict."""
        if self.success is None:
            return 3
        if self.success:
            return 0
        return 1 if self.decided else 4


RUNNING = Judgement(success=None, decided=None)
UNDECIDED = Judgement(success=False, decided=False)


def accepted_receipt(receipt: object) -> bool:
    """Whether a fixed-target receipt is the checker's acceptance of a named theorem."""
    return (
        isinstance(receipt, dict)
        and receipt.get("policy") == FIXED_TARGET_POLICY
        and isinstance(receipt.get("candidate_declaration"), str)
        and bool(receipt["candidate_declaration"])
    )


def judge_result(kind: object, result: object) -> Judgement:
    """The judgement of a completed operation of ``kind`` with this ``result``.

    The single rule every reader uses: reported results, the CLI's exit code and
    ``check --all``'s aggregate.
    """
    if not isinstance(result, dict):
        return UNDECIDED
    raw_status = result.get("status")
    status = raw_status if isinstance(raw_status, str) else None
    if kind in _BATCHES:
        return _judge_entries(result)
    if kind == "try_tactics":
        trials = result.get("results")
        if isinstance(trials, list):
            if not trials and result.get("skipped"):
                return UNDECIDED
            # A completed trial batch can contain unsuccessful candidate tactics.
            # This says nothing about whether the theorem has been proved.
            return Judgement(success=True, decided=True)
        return Judgement(success=False, decided=_actionable_error(result))
    if not isinstance(kind, str):
        return UNDECIDED
    answers = _ANSWERS.get(kind)
    if answers is None or status not in answers or not _well_formed(kind, status, result):
        return UNDECIDED
    return Judgement(success=_succeeded(kind, result), decided=True)


def judge_batch(result: Mapping[str, Any], *, entries: int) -> Judgement:
    """The judgement of a batch of ``entries`` from its status and counters.

    Decided only when the counters are non-negative integers that add up to
    the batch and its status follows from them.
    """
    raw = [result.get(name) for name in BATCH_COUNTERS]
    counts = [count for count in raw if type(count) is int and count >= 0]
    if len(counts) != len(raw) or sum(counts) != entries:
        return UNDECIDED
    if result.get("status") == "ok" and result["passed"] == entries:
        return Judgement(success=True, decided=True)
    if result.get("status") == "failed" and result["failed"] > 0:
        return Judgement(success=False, decided=True)
    return UNDECIDED


def _judge_entries(result: dict[str, Any]) -> Judgement:
    """A batch's judgement, whose counters must count exactly its entries."""
    entries = result.get("results")
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        return UNDECIDED
    tally = Counter(
        entry.get("status") for entry in entries if isinstance(entry.get("status"), str)
    )
    for name, statuses in BATCH_COUNTERS.items():
        if result.get(name) != sum(tally[status] for status in statuses):
            return UNDECIDED
    return judge_batch(result, entries=len(entries))


def _well_formed(kind: str, status: str, result: dict[str, Any]) -> bool:
    """Whether a tool's answer carries every field its status promises."""
    if status == "error":
        return _answered_error(kind, result)
    if kind in _LISTINGS:
        return isinstance(result.get(_LISTINGS[kind]), list)
    if kind == "inspect":
        return status not in _INSPECTED or isinstance(result.get("goal_contexts"), list)
    if kind == "suggest":
        if status == "closed":
            return result.get("verification") in SUGGESTION_VERIFICATIONS and isinstance(
                result.get("patch"), dict
            )
        return type(result.get("tried")) is int and isinstance(result.get("rejections"), list)
    if kind in REWRITE_SUCCESSES:
        return (
            isinstance(result.get("targets"), list)
            and type(result.get("source_changed")) is bool
            and (status != "rejected" or isinstance(result.get("reason"), str))
        )
    # A check or a fixed-target verification.
    if status == "ok" and kind == "verify_target":
        # Accepted only with the checker's receipt; without one, no verdict.
        return accepted_receipt(result.get("receipt"))
    if status == "rejected" and not _named_failure(result):
        return False
    return isinstance(result.get("diagnostics"), list)


def _named_failure(result: dict[str, Any]) -> bool:
    failure = result.get("failure")
    return isinstance(failure, dict) and isinstance(failure.get("kind"), str)


def _actionable_error(result: dict[str, Any]) -> bool:
    error = result.get("error")
    return (
        isinstance(error, dict)
        and isinstance(error.get("code"), str)
        and (error["code"] not in _STOPPED)
    )


def _answered_error(kind: str, result: dict[str, Any]) -> bool:
    """Whether a source tool's ``error`` is its answer, not a stopped preparation."""
    if "error" in result:
        return _actionable_error(result)
    # A listing of a file that does not compile is still a listing.
    return isinstance(result.get(_LISTINGS.get(kind, "diagnostics")), list)


def _succeeded(kind: str, result: dict[str, Any]) -> bool:
    status = result.get("status")
    if kind == "inspect":
        # Goal availability is separate from whether the worker retained a
        # proof state. Older services expose only that retention status.
        goals = result.get("goal_contexts")
        return (
            status in _INSPECTED
            and ("goal_status" not in result or result["goal_status"] == "available")
            and isinstance(goals, list)
            and bool(goals)
            and all(
                isinstance(goal, dict)
                and isinstance(goal.get("target"), str)
                and bool(goal["target"].strip())
                for goal in goals
            )
        )
    if kind == "suggest":
        # A closing tactic that changed nothing else, at one of the recheck tiers.
        return status == "closed"
    if kind in _LISTINGS:
        # A listing of a file that does not compile yet is still a listing;
        # its status and diagnostics say whether the file compiles.
        return isinstance(result.get(_LISTINGS[kind]), list)
    if kind in REWRITE_SUCCESSES:
        # Obligations are extracted, not proved; a failed or refused rewrite is not success.
        return status in REWRITE_SUCCESSES[kind]
    return status == "ok"


@dataclass(frozen=True)
class OperationOutcome:
    """A terminal or pending operation, including the unmodified server receipt."""

    operation: dict[str, Any]

    @property
    def terminal(self) -> bool:
        return self.operation.get("state") in {"completed", "failed", "cancelled"}

    @property
    def result(self) -> dict[str, Any] | None:
        envelope = self.operation.get("result")
        if not isinstance(envelope, dict):
            return None
        operation_id = self.operation.get("operation_id")
        revision = self.operation.get("revision")
        if (
            not isinstance(operation_id, str)
            or not operation_id.strip()
            or type(revision) is not int
            or revision < 0
            or envelope.get("operation_id") != operation_id
            or type(envelope.get("revision")) is not int
            or envelope["revision"] != revision
            or type(envelope.get("generation")) is not int
            or envelope["generation"] < 1
        ):
            return None
        result = envelope.get("result")
        return result if isinstance(result, dict) else None

    @property
    def judgement(self) -> Judgement:
        """What the operation established; see `judge_result`."""
        if not self.terminal:
            return RUNNING
        if self.operation.get("state") != "completed":
            # Failed or cancelled before a result: nothing was decided.
            return UNDECIDED
        return judge_result(self.operation.get("kind"), self.result)

    @property
    def verified(self) -> bool:
        return self.operation.get("kind") == "verify_target" and self.judgement.success is True

    @property
    def decided(self) -> bool:
        """Whether a finished operation reached a verdict, or a tool's answer."""
        return self.judgement.decided is True

    @property
    def successful(self) -> bool:
        """Proof rejection and diagnostics do not become success through HTTP 200."""
        return self.judgement.success is True


def reported(operation: dict[str, Any]) -> dict[str, Any]:
    """Lead an operation with whether it passed and whether it was decided.

    ``success`` and ``decided`` are true, false, or null while running. A failure
    that was decided is a wrong proof; one that was not is ours to retry.
    """
    judgement = OperationOutcome(operation).judgement
    return {"success": judgement.success, "decided": judgement.decided, **operation}
