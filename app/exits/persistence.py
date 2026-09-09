"""Pure checkpoint serialization/replay, NOT a database or durable outbox writer."""

from pydantic import Field

from .models import Record, Text, Event, ExitContractError, ExitSeed, ExitState, RecoveryRequired
from .policy import ExitPolicy
from .engine import initialize_exit, apply_event, digest


class ExitCheckpoint(Record):
    schema_version: str = Field(default='exit-checkpoint/v2',pattern=r'^exit-checkpoint/v2$')
    seed: ExitSeed
    policy: ExitPolicy
    journal: tuple[Event, ...]
    state: ExitState
    state_digest: Text


def checkpoint(seed: ExitSeed, policy: ExitPolicy, journal: tuple[Event, ...]) -> ExitCheckpoint:
    state=initialize_exit(seed,policy).state
    for event in journal:
        state=apply_event(seed,policy,state,event).state
    return ExitCheckpoint(seed=seed,policy=policy,journal=journal,state=state,state_digest=digest(state))


def restore_checkpoint(text: str, *, recovery_event: RecoveryRequired) -> ExitCheckpoint:
    """Verify complete replay, then append an explicit recovery fence.

    Persist the returned checkpoint before consuming its intents. Old INTENTs
    are reconciled by original IDs, never returned as newly sendable orders.
    Hashes detect content mismatch, not maliciously rewritten authenticated logs.
    """
    if type(text) is not str or type(recovery_event) is not RecoveryRequired:
        raise ExitContractError('Explicit checkpoint JSON and a recovery event are required')
    saved=ExitCheckpoint.model_validate_json(text)
    replayed=checkpoint(saved.seed,saved.policy,saved.journal)
    if saved!=replayed:
        raise ExitContractError('Checkpoint or journal mismatch; do not reset/clear state')
    if recovery_event.event_id in {key for key,_ in saved.state.event_receipts}:
        raise ExitContractError('Restart requires a new recovery event identity')
    return checkpoint(saved.seed,saved.policy,(*saved.journal,recovery_event))
