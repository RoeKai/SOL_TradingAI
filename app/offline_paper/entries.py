"""Opening-leg evidence, separate from transport ACKs and ExitState.

No receipt creates a fill. Terminal evidence is latched; known cumulative
quantity is a high-water mark, including already confirmed ledger details.
The local Broker audit detects missing deliveries, not just pending commands.
"""
from decimal import Decimal as D

from .models import OfflineError, amount
from .storage import get, rows, digest

TERMINAL = ('FILLED', 'CANCELED', 'REJECTED')


def detail_quantity(db, pid):
    return sum((amount(f['quantity']) for _, f in rows(db, 'fills')
                if f['position_id'] == pid and f['kind'] == 'ENTRY_FILL'), D(0))


def merge_evidence(reservation, details, *, status=None, cumulative=None):
    """Return new evidence without mutating input or manufacturing executions.

    Optional R1 diagnostic fields are derived for baseline 8A reservations;
    the original high-water/terminal fields remain required, never defaulted.
    ``entry_sealed`` is historical. Faults invalidate release, not that history.
    """
    r = dict(reservation)
    old = amount(r['entry_high_water'])
    terminal, terminal_qty = r['entry_terminal'], r['entry_terminal_quantity']
    if (terminal is None) != (terminal_qty is None) or terminal is not None and terminal not in TERMINAL:
        raise OfflineError('Invalid persisted opening terminal evidence')
    quantity = None if cumulative is None else amount(cumulative)
    known = max(old, details, quantity if quantity is not None else D(0))
    faults = list(r.get('entry_faults', ()))
    unknown = r.get('entry_status_unknown', False)
    if status is not None:
        if status not in (*TERMINAL, 'ACCEPTED', 'UNKNOWN'):
            raise OfflineError('Invalid opening receipt status')
        if status == 'UNKNOWN' or quantity is None:
            unknown = True
        elif quantity >= old:
            unknown = False
        if status in TERMINAL and quantity is not None:
            if terminal is None:
                terminal, terminal_qty = status, str(quantity)
            elif status != terminal or quantity != amount(terminal_qty):
                faults.append('ENTRY_TERMINAL_EVIDENCE_CONTRADICTION')
    if terminal is not None and known > amount(terminal_qty):
        faults.append('ENTRY_TERMINAL_CUMULATIVE_CONTRADICTION')
    if known > amount(r['quantity']):
        faults.append('ENTRY_CUMULATIVE_EXCEEDS_REQUEST')
    if terminal == 'FILLED' and amount(terminal_qty) != amount(r['quantity']):
        faults.append('ENTRY_FILLED_QUANTITY_MISMATCH')
    r.update(entry_high_water=str(known), entry_terminal=terminal,
             entry_terminal_quantity=terminal_qty, entry_status_unknown=unknown,
             entry_faults=list(dict.fromkeys(faults)))
    r['entry_reconciliation_required'] = bool(faults or unknown or known != details)
    if r['entry_reconciliation_required']:
        r['released'] = False
    return r


def audit_entry(db, pid, r):
    """Check original intent/order and all durable Broker facts under the lock.

    Returns (pending reasons, contradictions). A live accepted order can be
    reconciled but is NOT sealed. A CONFIRMED outbox item proves only delivery.
    This consumes no events and cannot alter cash, fees or position quantities.
    """
    key = r['entry_action_id']; item = get(db, 'outbox', key)
    pending = []; faults = list(r.get('entry_faults', ()))
    if (item is None or item['action'].get('kind') != 'ENTRY' or
        item['action'].get('action_id') != key or item['action'].get('position_id') != pid or
        item['action'].get('side') != ('BUY' if r['side'] == 'LONG' else 'SELL') or
        amount(item['action']['quantity']) != amount(r['quantity'])):
        return ['ENTRY_INTENT_UNRESOLVED'], [*faults, 'ENTRY_INTENT_BINDING_MISMATCH']
    command = get(db, 'broker_commands', key); order = get(db, 'broker_orders', key)
    facts = {fid: f for fid, f in rows(db, 'broker_fills') if f['position_id'] == pid and f['kind'] == 'ENTRY_FILL'}
    ledger = {fid: f for fid, f in rows(db, 'fills') if f['position_id'] == pid and f['kind'] == 'ENTRY_FILL'}
    if any(f['action_id'] != key for f in (*facts.values(), *ledger.values())):
        faults.append('ENTRY_FILL_ORDER_BINDING_MISMATCH')
    if any(fid not in facts or any(f.get(k) != v for k, v in facts[fid].items()) for fid, f in ledger.items()):
        faults.append('ENTRY_LEDGER_BROKER_FACT_CONTRADICTION')
    if facts.keys() != ledger.keys(): pending.append('ENTRY_FILL_DETAILS_MISSING')
    if command is None or order is None:
        pending.append('ENTRY_ORIGINAL_ORDER_NOT_CONFIRMED')
        if facts or ledger or order is not None or command is not None or item['status'] not in ('PENDING', 'INFLIGHT'):
            faults.append('ENTRY_ORIGINAL_ORDER_UNRESOLVED')
        return pending, list(dict.fromkeys(faults))
    if command['action_digest'] != digest(item['action']) or order['action'] != item['action']:
        faults.append('ENTRY_ORDER_INTENT_CONTENT_CONTRADICTION')
    total = sum((amount(f['quantity']) for f in facts.values()), D(0))
    cumulative = amount(order['cumulative']); known = amount(r['entry_high_water'])
    if cumulative != total: faults.append('ENTRY_BROKER_CUMULATIVE_FACT_CONTRADICTION')
    if known > cumulative: faults.append('ENTRY_BROKER_CUMULATIVE_BELOW_KNOWN')
    if known != cumulative: pending.append('ENTRY_CUMULATIVE_RECONCILIATION_REQUIRED')
    if r['entry_terminal'] is not None:
        if order['status'] != r['entry_terminal'] or cumulative != amount(r['entry_terminal_quantity']):
            faults.append('ENTRY_BROKER_TERMINAL_CONTRADICTION')
    elif order['status'] in TERMINAL:
        pending.append('ENTRY_TERMINAL_RECEIPT_MISSING')
    if r.get('entry_reconciliation_required') or r.get('entry_status_unknown'):
        pending.append('ENTRY_EVIDENCE_SETTLEMENT_REQUIRED')
    if order['status'] not in (*TERMINAL, 'ACCEPTED'):
        pending.append('ENTRY_ORDER_STATUS_UNKNOWN')
    return list(dict.fromkeys(pending)), list(dict.fromkeys(faults))
