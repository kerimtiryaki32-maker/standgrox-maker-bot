# Standgrox Maker Bot

**English** · [Türkçe](README_TR.md)

A local Windows dashboard for a single StandX perpetual market. It places two-sided post-only limit quotes at the chosen distance from mark price. Created by [@crryptooKerim](https://x.com/crryptooKerim).

> Experimental trading software. Live orders can fill despite early cancellation. Reduce-only market exits incur taker fees and slippage, and may be delayed or fail. Monitor your StandX account. Profit and Maker Hours are not guaranteed.

## Install

You need Windows 10/11, Python 3.10+, Node.js for initial EVM wallet login, and your own StandX account. Use a separate wallet with limited funds. Never enter your seed phrase.

```powershell
py -m pip install -r requirements.txt
npm install
Copy-Item .env.example .env
```

1. Run `node make_sign_key.js` and copy its `STANDX_SIGN_KEY_HEX=...` output to your **local** `.env`.
2. Add `EVM_WALLET_PRIVATE_KEY=...` to the local `.env` for login. This is highly sensitive. Never commit or share it. You can remove the line once the token is generated.
3. Run `node login.js`, then copy `STANDX_TOKEN=...` to `.env`. If authentication returns 401, log in again to refresh the token. Keep the signing key for the same session.
4. Run `py main.py` to see startup errors or double-click `run_app.pyw` for a console-free window. `create_shortcut.bat` optionally creates an icon-bearing desktop shortcut.
5. On Windows, `build_windows.bat` optionally installs PyInstaller and creates `dist/Standgrox Maker Bot.exe`. Place your private `.env` beside the EXE. The EXE build has not been verified here on Windows.

The app starts in **simulation** mode. Select a market, target/min/max BPS, balance usage (up to 100%), leverage, and emergency loss threshold. The original panel layout and settings remain. With defaults of target 5.5 and minimum 5 BPS, approaching orders are pulled at or below 5.25 BPS from mark. The bot also pulls an order when the executable opposite book approaches within 1 BPS. It polls every three seconds and has no 10-second replacement delay. For real orders, check **Enable live orders** and confirm. The bot may change account leverage if there are no open orders or positions on that market. At 100% balance usage the exchange may reject a quote if additional fee reserves are needed.

On a detected fill, it cancels the other bot order and tries a reduce-only post-only limit exit just inside the best ask (long) or best bid (short). If it is not fully closed in 5 seconds, it confirms cancellation and sends a reduce-only market IOC for the remaining position; reaching the selected emergency loss threshold also uses a market exit. If a submitted exit or cancellation cannot be verified, the bot stops instead of blindly duplicating an exit. Maker exits may remain open and are not guaranteed to execute. Three seconds is the idle recheck interval, not a guaranteed replacement time. The WebSocket listener subscribes to mark price, full depth book, orders and positions. A separate entry guard wakes on market updates and checks feed freshness every 100 ms. It requests cancellation when mark reaches the minimum BPS plus early-pull buffer, the quote exceeds maximum BPS, or the executable opposite book comes within 1 BPS. Entry creation rechecks the live snapshot before submission. Missing/invalid data or a feed with local receipt age above 3 seconds blocks new entries and requests cancellation of existing entries. The 100 ms check is not a guaranteed exchange cancellation time; HTTP writes are serialized and accepted cancels still need terminal confirmation. A brief gap while obtaining a safe quote is intentional.

Target BPS places bid and ask symmetrically around mark; Minimum/Maximum BPS determine when existing quotes are refreshed. An approaching order is canceled even if a safe replacement is temporarily unavailable. The panel's rolling 60-minute two-sided uptime is a conservative local estimate from observed open orders, not official StandX Maker Hours. StandX requires both sides within 10 bps for at least 30 minutes per hour (42 minutes for the boosted tier). Fast markets, partial fills, API delays, and rejection can interrupt eligibility.

If only one entry order remains visible, the bot retries the missing side each scan. After 15 seconds, it verifies pending orders, cancels the lone order, and rebuilds both quotes. If a delayed order or cancellation cannot be verified, it waits rather than placing a duplicate. This improves recovery but cannot guarantee two-sided uptime or prevent fills.

**Stop** cancels and verifies bot-owned open orders. If an entry filled during this bot session, it attempts the maker-first exit described above; an unconfirmed close still requires immediate inspection in StandX. Positions already open before startup are never traded automatically. Other strategies' orders remain untouched.

If the WebSocket mark or book is missing or delayed, an independent backup worker retries fresh public HTTP snapshots at one-second intervals. Slow or invalid snapshots remain blocked; the three-second age limit is measured from the start of the HTTP read. Newer WebSocket values are preserved. Connection errors and backup readiness appear in Activity log. The backup is slower than streaming and cannot prevent fills.

## Files and privacy

GitHub should contain source files, documentation, and the empty `.env.example`. Never publish `.env`, wallet keys, JWTs, signing keys, `build/`, `dist/`, `__pycache__/`, `node_modules/`, or `.spec` files. `.gitignore` excludes them by default, but check `git status` before every commit. If a secret was ever committed, deleting the file later does not revoke it: rotate the keys and token.

This project has not been validated on a live account in the authoring environment. Review the current [Perps HTTP API](https://docs.standx.com/standx-api/perps-http) and [EVM authentication example](https://docs.standx.com/standx-api/perps-auth-evm-example) before live use.
