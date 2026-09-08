"""Common interface every broker integration implements.

trade_monitor's signal generation is broker-agnostic; only order placement
and account/position reads differ per broker. Concrete adapters (e.g.
ZerodhaAdapter) wrap an existing session client (KiteSession, CapitalSession)
rather than reimplementing broker calls — this file only defines the shape
callers can rely on regardless of which broker a client has configured.

Prices/quantities are always in the broker's native units (Zerodha: integer
share quantity; Capital.com: CFD size) — this layer does not normalize
sizing, only the call surface.
"""

from abc import ABC, abstractmethod


class BrokerAdapter(ABC):
    name: str  # short id, e.g. "zerodha", "capital"

    @abstractmethod
    def account_summary(self):
        """Returns a dict with at least: available_margin/available_funds,
        used_margin (0 if not applicable to this broker)."""

    @abstractmethod
    def positions(self):
        """Returns a list of open positions as dicts with at least:
        symbol, quantity, side ('BUY'/'SELL'), entry_price, current_price,
        pnl, broker_position_id (opaque id used by close_position)."""

    @abstractmethod
    def place_entry_order(self, symbol, side, quantity, order_type="MARKET", price=None):
        """Places the entry leg. Returns a dict with at least:
        broker_order_id, status."""

    @abstractmethod
    def place_stop_target(self, symbol, side, quantity, stop_price, target_price, entry_price=None):
        """Attaches (or places as a separate OCO order, per broker) stop-loss
        and take-profit protection for an open position. Returns a dict with
        at least: broker_protection_id (used to later cancel/replace it)."""

    @abstractmethod
    def cancel_stop_target(self, broker_protection_id):
        """Cancels previously placed stop/target protection, e.g. before
        replacing it with a moved (breakeven) stop."""

    @abstractmethod
    def close_position(self, broker_position_id):
        """Immediately flattens one open position at market."""

    @abstractmethod
    def order_status(self, broker_order_id):
        """Returns the current status string for a previously placed order
        (e.g. COMPLETE / REJECTED / CANCELLED / OPEN — broker-native, callers
        that need a normalized status should map it themselves)."""
