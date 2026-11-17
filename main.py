"""Fade-the-dip BTC bot for agent-fun/bars-replay/v1.

Idea, measured on the kit's 1440-bar dev window before this code was written:
the window is strongly mean-reverting at the 30-120 minute horizon. After a
trailing 60-minute return above +100 bps, the next 60 minutes average -101 bps
(positive only 3% of the time); fast trend systems bleed out on the round-trip
cost (about 36 bps at full size: 4 bps fee + 2 bps slippage + ~11 bps impact
each way), while buy-and-hold makes only +1.07 USDC on the dev window.
So this bot does the opposite of chasing: it buys a sharp dip and sells the
recovery, a few times per day at most.

Rules (all thresholds in basis points, all decisions from bar closes only):
- Enter: flat, at least 240 bars of history, trailing 60-bar return below
  -100 bps, and the trailing 240-bar return above -250 bps (regime filter:
  do not fade a day that is already in a deep multi-hour slide).
  Buy with all cash.
- Exit: sell everything when the close is +100 bps above the fill price, or
  the trailing 60-bar return has swung above +100 bps, or the close is
  -200 bps below the fill price (stop-loss), or 240 bars have passed since
  the fill (time-stop).
- Fallback: if no trade has happened by bar 1320 and the trailing 60-bar
  return is below -50 bps, buy 25% of cash once, so a calm window still
  records a trade. Same exit rules apply.

Costs are why the size is full, not split: the fee and slippage are fixed
per round trip, and the impact saving from halving the size is smaller than
the profit given up when a 100+ bps recovery is caught. The stop-loss and
time-stop bound the loss if a dip keeps falling instead of reverting.

Determinism: no clock, no environment, no randomness. Every decision is a
pure function of the bars received and the account state the harness sends.
Orders fill at the next bar's open; max_slippage_bps 300 means a fill more
than 3% away from the latest close is refused, which is also the protection
against buying into a gap-up that erased the dip.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# Entry: trailing 60-bar return below -DIP_BPS, with a 240-bar regime floor.
DIP_BPS = 100
REGIME_R240_BPS = -250
HISTORY_BARS = 240
# Exit: recovery above entry, momentum swing, stop-loss, time-stop.
REC_BPS = 100
EXIT_R60_BPS = 100
STOP_BPS = 200
TIME_STOP_BARS = 240
# Fallback: one small late trade so a calm window still records a trade.
FALLBACK_BAR = 1320
FALLBACK_R60_BPS = 50
FALLBACK_NUM = 1
FALLBACK_DEN = 4
# A fill more than 3% from the latest close is refused.
MAX_SLIPPAGE_BPS = 300


def _bps(now, then):
    """Return of now vs then in basis points (float, deterministic)."""
    return (now / then - 1.0) * 10000.0


class FadeBot:
    def __init__(self):
        # Close prices in USD per BTC, one per bar received (warmup included).
        self.closes = []
        # Fill price (USD) and batch seq of the current position's entry.
        self.entry_price = None
        self.entry_seq = None
        # How many of our own buy orders have filled so far.
        self.trades = 0
        # Fallback bookkeeping: one-shot unless its order was rejected.
        self.fallback_done = False
        self.fallback_cid = None
        self._n = 0

    def _close(self, state):
        bar = state.latest.get(SYMBOL)
        if bar is None:
            return None
        scale = state.symbols[SYMBOL]["price_scale"]
        return int(bar["c"]) / 10.0 ** scale

    def _record_fills(self, state, batch):
        for fill in batch.get("fills", []):
            cid = fill.get("client_id", "")
            if not cid.startswith("fade-"):
                continue
            if fill.get("status") == "rejected" and cid == self.fallback_cid:
                # A rejected fallback may be tried again on a later bar.
                self.fallback_done = False
                self.fallback_cid = None
            if fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                scale = state.symbols[SYMBOL]["price_scale"]
                self.entry_price = int(fill["price"]) / 10.0 ** scale
                self.entry_seq = state.seq
                self.trades += 1

    def _holding(self, state):
        pos = state.positions.get(SYMBOL)
        return pos is not None and pos.qty > 0

    def on_batch(self, state, batch):
        self._record_fills(state, batch)
        # Only trade on a fresh bar: if this batch carries no bar for the
        # symbol, state.latest still holds the previous bar, so a decision
        # here would repeat stale data. Fills were already recorded above.
        if not any(b.get("symbol") == SYMBOL for b in batch.get("bars", [])):
            if not self._holding(state):
                self.entry_price = None
                self.entry_seq = None
            return []
        close = self._close(state)
        if close is None:
            return []
        self.closes.append(close)
        if state.warming_up:
            return []
        n = len(self.closes)
        if self._holding(state):
            return self._maybe_exit(state, close, n)
        # Flat: drop any entry metadata left over from a closed position,
        # so a later position never inherits a stale fill price or timing.
        self.entry_price = None
        self.entry_seq = None
        return self._maybe_enter(state, close, n)

    def _maybe_exit(self, state, close, n):
        entry = self.entry_price
        if entry is None:
            # Fill not seen (defensive): derive average cost from the account.
            pos = state.positions[SYMBOL]
            entry = int(pos.cost) * 100.0 / pos.qty
        r60 = _bps(close, self.closes[n - 61]) if n > 60 else 0.0
        held = state.seq - self.entry_seq if self.entry_seq is not None else 0
        if (
            close >= entry * (1.0 + REC_BPS / 10000.0)
            or r60 > EXIT_R60_BPS
            or close <= entry * (1.0 - STOP_BPS / 10000.0)
            or held >= TIME_STOP_BARS
        ):
            self._n += 1
            return [
                {
                    "side": "sell",
                    "client_id": "fade-sell-%d" % self._n,
                    "symbol": SYMBOL,
                    "qty": state.positions[SYMBOL].qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }
            ]
        return []

    def _maybe_enter(self, state, close, n):
        if n <= HISTORY_BARS:
            return []
        r60 = _bps(close, self.closes[n - 61])
        r240 = _bps(close, self.closes[n - 241])
        cash = state.cash
        min_notional = int(state.hello["fees"]["min_notional_quote_units"])
        if r60 < -DIP_BPS and r240 > REGIME_R240_BPS and cash >= min_notional:
            self._n += 1
            return [
                {
                    "side": "buy",
                    "client_id": "fade-buy-%d" % self._n,
                    "symbol": SYMBOL,
                    "notional_quote_units": cash,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }
            ]
        # Fallback: one small late trade if the window stayed calm.
        if (
            self.trades == 0
            and not self.fallback_done
            and state.seq >= FALLBACK_BAR
            and r60 < -FALLBACK_R60_BPS
        ):
            amount = cash * FALLBACK_NUM // FALLBACK_DEN
            if amount >= min_notional:
                self._n += 1
                self.fallback_done = True
                self.fallback_cid = "fade-buy-%d" % self._n
                return [
                    {
                        "side": "buy",
                        "client_id": self.fallback_cid,
                        "symbol": SYMBOL,
                        "notional_quote_units": amount,
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    }
                ]
        return []


def main():
    bot = FadeBot()
    makefun_bars.run(bot.on_batch)


if __name__ == "__main__":
    main()
