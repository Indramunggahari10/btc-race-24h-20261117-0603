# Strategy: fade the dip

## What the dev window showed

The kit's 1440-bar dev window (BTC 64,461 -> 64,776 USDC, +0.49%) is
mean-reverting at the 30-120 minute horizon, not trending:

- 1-minute log-return std 6.4 bps; mean |1-minute move| 4.3 bps.
- After a trailing 60-minute return above +100 bps, the next 60 minutes
  average -101 bps (positive only 3% of 38 overlapping samples).
- Fast trend systems bleed out: EMA-cross 8/21 loses -106.94 USDC in 68
  trades, Donchian 120/30 loses -19.26, momentum 120m loses -9.74.
  A full-size round trip costs ~36 bps (4 bps fee + 2 bps slippage +
  ~11 bps impact each way), so a trade must catch a move well above that.
- Buy-and-hold makes +1.07 USDC (official local grader: +1.067187).

## The bot

Buy with all cash when the trailing 60-bar return is below -100 bps and the
trailing 240-bar return is above -250 bps (regime filter: never fade a day
already in a deep multi-hour slide). Sell everything when the close is
+100 bps above the fill price, or the trailing 60-bar return has swung above
+100 bps, or the close is -200 bps below the fill price (stop-loss), or 240
bars have passed since the fill (time-stop). If no trade has happened by bar
1320 and the trailing 60-bar return is below -50 bps, buy 25% of cash once,
so a calm window still records a trade.

## Measured results (dev window)

- Offline simulator (fill model validated against the official grader on
  buy-and-hold: +1.07 simulated vs +1.067187 graded): **+20.35 USDC**,
  3 round trips, max drawdown -0.72%.
- The real `main.py`, run through a Python driver implementing the
  contract's fill rules with integer math: **+20.349537 USDC**, identical
  action logs across two runs, 0 rejected orders.
- Parameter grid (dip 70-120 bps, recovery 70-120 bps, windows 30/60/90/120
  minutes) is all positive (+0.71 to +26.99 USDC); the shipped centre
  (100/100, 60 minutes) is +20.35, not the grid peak (+26.99 at 110/120).
- House baselines on the same window (official grader): buy-and-hold +1.07,
  momentum +2.45, ema-cross -106.21, rsi-bollinger -24.25, adx-momentum
  -32.66, hold-cash 0.00. This bot beats the best house baseline by ~8x.

## Honest limits

The edge rests on 3 trades in one 24-hour window; the +20.35 figure is
almost certainly flattered by a benign regime. The stop-loss (-200 bps) and
time-stop (240 bars) bound the loss if a dip keeps falling, and the regime
filter refuses to fade a deep slide, but a trending race window could still
produce few or losing trades. Four independent second opinions (relays)
reviewed this design before it was frozen; their advice that was verified
by measurement was adopted (central parameters, stop-loss, time-stop,
regime filter, small late fallback), and their suggestions that measured
worse on the dev window were rejected with numbers (earlier exit at +50 bps
60-minute return: +4.67; fast-dip filter at 15m < -40 bps: +16.35;
half-size entries: +11.96; staggered entry: +13.66). A second relay round
reviewed the near-final code: it caught one real bug (the fallback
threshold's sign, which would have let the fallback buy on rising prices;
fixed and unit-tested) plus the time-stop boundary and stale-entry
bookkeeping; three other review claims were checked against the harness
source and the arithmetic and rejected as incorrect (the cost-derived entry
price formula, the fill-timing accounting, and a supposed duplicate-sell
race that the fill reporting makes impossible).
