"""BrokerAdapter implementation wrapping the existing capital_api.CapitalSession.

Wraps, not replaces, CapitalSession — auto_trader.py's existing flow is
untouched. Capital.com is CFD-style: positions are addressed by dealId
(not symbol), stop/target are attached directly on create_position/
update_position rather than a separate OCO order like Zerodha's GTT.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker_adapter import BrokerAdapter
from capital_api import CapitalSession


class CapitalAdapter(BrokerAdapter):
    name = "capital"

    def __init__(self, session=None):
        self.session = session or CapitalSession()
        self._epic_by_position_id = {}

    def account_summary(self):
        accounts = self.session.accounts()
        primary = next((a for a in accounts if a.get("preferred")), accounts[0] if accounts else {})
        balance = primary.get("balance", {})
        return {
            "available_margin": balance.get("available", 0),
            "used_margin": balance.get("deposit", 0),
        }

    def positions(self):
        raw = self.session.positions() or []
        out = []
        for p in raw:
            pos, market = p.get("position", {}), p.get("market", {})
            deal_id = pos.get("dealId")
            epic = market.get("epic")
            self._epic_by_position_id[deal_id] = epic
            direction = pos.get("direction")
            current = market.get("bid") if direction == "SELL" else market.get("offer")
            out.append({
                "symbol": epic,
                "quantity": pos.get("size"),
                "side": direction,
                "entry_price": pos.get("level"),
                "current_price": current,
                "pnl": pos.get("upl"),
                "broker_position_id": deal_id,
            })
        return out

    def place_entry_order(self, symbol, side, quantity, order_type="MARKET", price=None):
        result = self.session.create_position(epic=symbol, direction=side, size=quantity)
        deal_ref = result.get("dealReference")
        confirm = self.session.confirm_deal(deal_ref) if deal_ref else {}
        return {"broker_order_id": confirm.get("dealId") or deal_ref, "status": confirm.get("dealStatus", "SUBMITTED")}

    def place_stop_target(self, symbol, side, quantity, stop_price, target_price, entry_price=None):
        """Capital.com attaches stop/target on the position itself rather than
        a separate order — this expects the broker_position_id (dealId) to
        be passed as `symbol` here, since that's what update_position needs."""
        result = self.session.update_position(symbol, stop_level=stop_price, profit_level=target_price)
        return {"broker_protection_id": symbol, "raw": result}

    def cancel_stop_target(self, broker_protection_id):
        return self.session.update_position(broker_protection_id, stop_level=None, profit_level=None)

    def close_position(self, broker_position_id):
        return self.session.close_position(broker_position_id)

    def order_status(self, broker_order_id):
        confirm = self.session.confirm_deal(broker_order_id)
        return confirm.get("dealStatus", "UNKNOWN")
