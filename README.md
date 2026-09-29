# Standgrox Maker Bot

**English** · [Türkçe](README_TR.md)

A local Windows dashboard for a single StandX perpetual market. It places two-sided post-only limit quotes at the chosen distance from mark price. Created by [@crryptooKerim](https://x.com/crryptooKerim).

> Experimental trading software. Live orders can fill despite quote refreshes. Reduce-only market exits may incur taker fees and slippage or fail to close fully. Monitor your StandX account. Profit and Maker Hours are not guaranteed.

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

The app starts in **simulation** mode. Select a market, target/min/max BPS, balance usage and leverage. For real orders, check **Enable live orders** and confirm. The bot may change account leverage if there are no open orders or positions on that market.

The bot checks positions approximately once a second, pulls quotes as the mark approaches their minimum BPS distance, and submits a reduce-only **market** order after detecting a fill. It confirms the position reaches zero before resuming. A fast move, API delay, rejection, partial fill or network failure may still leave a position open. Market exits incur taker fees and slippage. If an order disappears unexpectedly or an exit is not confirmed, the bot stops sending entries; inspect StandX immediately.

**Stop** cancels the bot's open orders. If a bot entry filled during the session, it attempts a reduce-only market close and checks the resulting position. Existing positions from before bot startup are not closed automatically. Other strategies' orders remain untouched. Always confirm the actual position and order state in StandX after stopping.

## Files and privacy

GitHub should contain source files, documentation, and the empty `.env.example`. Never publish `.env`, wallet keys, JWTs, signing keys, `build/`, `dist/`, `__pycache__/`, `node_modules/`, or `.spec` files. `.gitignore` excludes them by default, but check `git status` before every commit. If a secret was ever committed, deleting the file later does not revoke it: rotate the keys and token.

This project has not been validated on a live account in the authoring environment. Review the current [Perps HTTP API](https://docs.standx.com/standx-api/perps-http) and [EVM authentication example](https://docs.standx.com/standx-api/perps-auth-evm-example) before live use.
