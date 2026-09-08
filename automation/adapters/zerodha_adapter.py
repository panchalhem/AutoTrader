"""BrokerAdapter implementation wrapping the existing zerodha_api.KiteSession.

This wraps, not replaces, KiteSession — stock.sh/watchdog.sh and the existing
zerodha_trader.py flows keep using KiteSession directly unchanged. This
adapter exists for new callers (the FastAPI service, future multi-broker
clients) that want the broker-agnostic BrokerAdapter surface instead.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker_adapter import BrokerAdapter
from zerodha_api import KiteSession


class ZerodhaAdapter(BrokerAdapter):
    name = "zerodha"

    def __init__(self, session=None, exchange="NSE", default_product="CNC"):
        self.session = session or KiteSession()
        self.exchange = exchange
        self.default_product = default_product

    def account_summary(self):
        margins = self.session.margins()
        equity = (margins or {}).get("equity", {})
        available = equity.get("available", {})
        utilised = equity.get("utilised", {})
        return {
            "available_margin": available.get("live_balance", 0),
            "used_margin": utilised.get("debits", 0),
        }

    def positions(self):
        raw = self.session.positions() or {}
        out = []
        for p in raw.get("net", []):
            qty = p.get("quantity", 0)
            if qty == 0:
                continue
            out.append({
                "symbol": p.get("tradingsymbol"),
                "quantity": abs(qty),
                "side": "BUY" if qty > 0 else "SELL",
                "entry_price": p.get("average_price"),
                "current_price": p.get("last_price"),
                "pnl": p.get("pnl"),
                "broker_position_id": p.get("tradingsymbol"),
            })
        return out

    def place_entry_order(self, symbol, side, quantity, order_type="MARKET", price=None):
        result = self.session.place_order(
            self.exchange, symbol, side, quantity,
            order_type=order_type, product=self.default_product, price=price,
        )
        return {"broker_order_id": result, "status": "SUBMITTED"}

    def place_stop_target(self, symbol, side, quantity, stop_price, target_price, entry_price=None):
        exit_side = "SELL" if side == "BUY" else "BUY"
        last_price = entry_price if entry_price is not None else stop_price
        result = self.session.place_gtt_oco(
            self.exchange, symbol, last_price, quantity, exit_side,
            stop_price, target_price, product=self.default_product,
        )
        trigger_id = (result or {}).get("trigger_id")
        return {"broker_protection_id": trigger_id}

    def cancel_stop_target(self, broker_protection_id):
        return self.session.delete_gtt(broker_protection_id)

    def close_position(self, broker_position_id):
        positions = self.positions()
        match = next((p for p in positions if p["broker_position_id"] == broker_position_id), None)
        if not match:
            raise ValueError(f"No open Zerodha position found for {broker_position_id}")
        exit_side = "SELL" if match["side"] == "BUY" else "BUY"
        return self.session.place_order(
            self.exchange, broker_position_id, exit_side, match["quantity"],
            order_type="MARKET", product=self.default_product,
        )

    def order_status(self, broker_order_id):
        history = self.session.order_history(broker_order_id)
        if not history:
            return "UNKNOWN"
        return history[-1].get("status", "UNKNOWN")
