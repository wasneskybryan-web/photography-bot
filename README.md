# Photo Shop Bot

A Telegram bot that sells photographers' bundles and premium-channel access for **Telegram Stars**, and keeps a ledger of what each photographer has earned and been paid.

## What customers see
- **Browse bundles**: photographer → bundle → **Buy for ⭐N**. After paying they get the download link right away, and it stays saved under **My purchases**.
- **Premium 4K channel**: either a monthly Stars subscription (renews automatically, and they can cancel inside Telegram) or a one-time purchase. After paying, they get a **personal invite link**. The bot only approves the person who bought it, so a link that gets shared is useless to anyone else.
- When a monthly subscription lapses (with a 24h grace period), the bot removes the member from the channel and sends them a renew button.
- Deep links you can post anywhere: `t.me/YOURBOT?start=b_alex-sunsets` or `t.me/YOURBOT?start=premium`

## Commands
| Command | Who | What it does |
|---|---|---|
| `/balances` | admins | Each photographer's earned / held (21-day hold) / paid / owed, in Stars |
| `/payout bea 1200 paid via Zelle` | admins | Record that you paid someone. Notifies them. |
| `/sales [days]` | admins | Recent sales with charge ids |
| `/refund <charge_id>` | admins | Refunds the Stars. For premium, it also cancels renewal and removes them from the channel. |
| `/reload` | admins | Re-read `config.json` |
| `/backup` | admins | DMs you a copy of the ledger database |
| `/mysales` | photographers | Their own balance and last 30 days |
| `/id` | anyone | Shows your Telegram user id |
| `/paysupport`, `/terms` | customers | Telegram requires `/paysupport` for bots that take Stars |

Admins and the photographer get a DM on every sale.

## How money flows
1. A customer pays in Stars, and the whole amount lands in the bot's Stars balance.
2. Each sale is split when it happens. A bundle goes 100% to its photographer, and premium is split by `premium.split`. There's an optional `house_cut_percent` for running costs.
3. Telegram holds Stars for 21 days, and you need 1,000+ withdrawable Stars to cash out. Whoever owns the bot in @BotFather withdraws through Fragment as TON, then sells it on an exchange.
4. Pay each photographer what `/balances` says is **owed**, then record it with `/payout`.

The ledger is `shop.db` on the Railway volume. Run `/backup` every week or two and keep the file somewhere safe.

---

## Setup

### 1. Telegram
1. Message @BotFather, send `/newbot`, and copy the **token**.
2. Create the premium channel as **private** and revoke any existing invite links.

### 2. GitHub
1. Copy `config.example.json` to `config.json` and fill in the shop name, bundles, prices and download URLs. Leave the ids for now.
2. Push to a **private** repo:
   ```bash
   git init && git add . && git commit -m "photo shop bot"
   git branch -M main
   git remote add origin https://github.com/YOURNAME/photo-shop-bot.git
   git push -u origin main
   ```

### 3. Railway
1. Click **New Project → Deploy from GitHub repo** and pick the repo. The first deploy fails until you add the token, which is expected.
2. **Variables** tab: add `BOT_TOKEN` = the token from BotFather. That's the only variable.
3. **Volume:** press ⌘K / Ctrl+K and choose **Create Volume**. Attach it to the bot service with mount path **`/data`**. Don't use `/app`.
4. **Settings:**
   - **Custom Start Command:** `python main.py`
   - **Restart Policy:** Always
   - **App Sleeping / Serverless:** off
   - No public domain needed
5. Deploy and check **Deploy Logs** for:
   ```
   ledger database: /data/shop.db
   starting ...
   ```

### 4. Finish the config
1. You and each photographer open the bot and send `/id`. Put the numbers in `admins` and in each photographer's `telegram_id`.
2. Add the bot to the premium channel as an **admin** with *Invite users via link* and *Ban users*. It will DM you the `channel_id`.
3. Put `channel_id` in `config.json`, then commit and push. Railway redeploys by itself.
4. **Test:** set a bundle to 1 Star, buy it yourself, check `/sales` and `/balances`, then `/refund` it.

## Day to day
- **Changing bundles or prices:** edit `config.json` and push. A redeploy takes a few seconds of downtime, and nobody is charged if a payment is attempted during it.
- **Never run a second copy** with the same token (for example on your PC). Telegram only lets one copy receive updates. Make a separate test bot instead.

## Troubleshooting
| Log line | Fix |
|---|---|
| `Running on Railway WITHOUT a volume` | Attach a volume at `/data` |
| `Set BOT_TOKEN` | Add the `BOT_TOKEN` variable |
| `Conflict: terminated by other getUpdates request` | Another copy of the bot is running with the same token |
| `Invite link failed ... Is the bot an admin` | Give the bot admin rights in the channel |

## Files
- `bot.py`: the bot
- `ledger.py`: SQLite ledger
- `main.py`: entry point for Railway
- `config.example.json`: copy this to `config.json`
- `test_ledger.py`: `python3 -m unittest test_ledger`
- `photo-shop-bot.service`: only needed if you ever move it to your own Linux server instead of Railway
