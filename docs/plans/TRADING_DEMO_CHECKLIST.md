# Trading guard: demo-account checklist (do this once before any real order)

The guard (dourmouse/order_guard.py) is tested only against fake brokers. Nobody has contacted Trading 212 or MetaTrader 5 from this code. Do these on a DEMO account, with small sizes, and write PASS or FAIL next to each.

## Trading 212 (practice account)
1. Open `GET /api/v0/equity/orders` and `GET /api/v0/equity/portfolio` with your practice key (curl or the T212 docs "try it"); paste one real answer of each (remove the key) into a note. The guard reads fields named like `totalValue`/`total`/`equity`, `ticker`, `quantity`, `currentPrice`, and for orders `ticker`, `quantity`, `type`, `limitPrice`, `stopPrice` (guesses, FIXR4 question Q1). If a name differs, the guard refuses with "could not read the account"; tell me the real names and I fix the reader.
2. Ask HOME to buy 1 share of a cheap stock with a LIMIT order well below the market. Expect: a confirmation card naming DEMO, side, size, type, limit, stop. Approve. Expect the order to appear in the T212 app as pending. If the broker rejects it with a 404 or 405, the order endpoint shape is wrong (Q: one endpoint per order type); note the exact error.
3. With that order still pending, ask for a second identical order until the total would exceed 10 percent of the account: expect a refusal that mentions the queued order.
4. Cancel the pending order in the T212 app. Ask HOME to place a MARKET buy for an unheld ticker: expect a refusal asking for a LIMIT order (no reference price). Place a LIMIT buy that fills; then ask to sell part of it with a MARKET order: expect it to pass (a reduction).
5. Ask for a STOP order on a ticker you do not hold: expect a refusal. Report what T212 itself says if you try a buy STOP below the market in the T212 app (Q4).

## MetaTrader 5 demo
1. Run the MT5 demo terminal. Ask HOME for the account margin mode (hedging or netting) and the open positions; the tool reply should say which. On a hedging account a plain SELL must count as new exposure; closing needs the position ticket.
2. Place a tiny buy (0.01 lot) on a liquid symbol; if it is refused for exceeding 10 percent on a small demo account, set `DOURMOUSE_MAX_POSITION_PCT` higher for the test (say 50) and record that.
3. Close it with the position ticket: expect it to pass even after tripping the kill switch (test: lower `DOURMOUSE_MAX_DAILY_LOSS_PCT` to a tiny value, place any order to trip, then close).
4. Kill the MT5 terminal mid-order if you can: expect "STATUS UNKNOWN, check positions before retrying".

## The kill switch
1. Trip it (item above). Confirm new exposure is refused and a reduction passes.
2. In a Terminal run `python -m dourmouse.order_guard status` (path printed), then `python -m dourmouse.order_guard rearm`, type REARM: expect it to clear, and to say the baseline is unchanged and the switch trips again if the loss still stands. `--reset-baseline` resets the day's baseline on purpose.
3. Try the same rearm through HOME (ask the model to run it): it must fail.

Open owner questions (my recommendations): keep passing a plain reduction when the pending-order list is unreadable (yes); queued MARKET BUY for an unheld ticker blocks every non-reduction until it fills or is cancelled (acceptable); 30 s lock wait and 60 s MT5 timeout (fine).
