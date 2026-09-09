"""Explicit optional records for offline assembly checks; missing stays missing."""

from app.admission.models import AdmissionDecision, AdmissionRequest, ExchangeConstraints, PaperRiskSnapshot
from app.exits.models import ExitSeed, ExitState, ExitVenueRules
from app.exits.policy import ExitPolicy
from app.setups.models import Record, TradeSetup
from app.setups.rr_models import RRCalculation
from app.setups.scorecard_models import Scorecard
from .models import PlanConfigurationBinding


class BoundPosition(Record):
    seed: ExitSeed
    policy: ExitPolicy
    state: ExitState


class PlanInputs(Record):
    setup: TradeSetup
    rr: RRCalculation | None = None
    scorecard: Scorecard | None = None
    admission: AdmissionDecision | None = None
    configuration_binding: PlanConfigurationBinding | None = None
    account: PaperRiskSnapshot | None = None
    exchange: ExchangeConstraints | None = None
    request: AdmissionRequest | None = None
    exit_rules: ExitVenueRules | None = None
    bound_position: BoundPosition | None = None
