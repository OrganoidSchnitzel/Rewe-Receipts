# Telegram notifications — setup guide

Get a push to your phone the moment a new receipt is imported, and (optionally)
**approve the Spliit expense straight from the chat** with a button — no need to
open the web UI.

Everything is **outbound-only**: the service talks *out* to Telegram's API. You
do **not** need to expose any port, set up a public URL, or configure an inbound
webhook — it works fine behind your home router/NAT. The two-way buttons use
long-polling under the hood.

---

## 1. Create a bot and get its token

1. In Telegram, open a chat with **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot`.
3. Give it a **name** (any display name, e.g. `Receipt Importer`).
4. Give it a **username** ending in `bot` (must be unique, e.g. `my_receipts_bot`).
5. BotFather replies with a **token** that looks like:

   ```
   8123456789:AAE...long...string
   ```

   Copy it — this is your `TELEGRAM_BOT_TOKEN`.

> Keep the token secret. Anyone with it can control the bot. If it leaks, send
> `/revoke` to BotFather to get a new one.

## 2. Start the bot and get your chat id

The bot can only message you **after you message it first**.

1. Open the chat with your new bot (tap the `t.me/<username>` link BotFather
   gave you) and press **Start** (or send any message, e.g. `hi`).
2. Get your numeric chat id — two easy ways:

   **A. Via the API (no extra bots):** open this URL in a browser, replacing
   `<TOKEN>`:

   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

   Look for `"chat":{"id":123456789,...}`. That number is your
   `TELEGRAM_CHAT_ID`. (If the result is empty, send your bot another message
   and refresh.)

   **B. Via a helper bot:** message **[@userinfobot](https://t.me/userinfobot)**;
   it replies with your id.

> A personal chat id is a positive number. To send to a **group** instead, add
> the bot to the group, post a message there, and use the group id from
> `getUpdates` (group ids are negative, e.g. `-100…`).

## 3. Configure the service

In your `.env`:

```ini
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=8123456789:AAE...long...string
TELEGRAM_CHAT_ID=123456789

# Needed for the "Open / Review" link and the two-way "Approve" button to point
# back at the app. Set it to however YOU reach the app on your LAN:
APP_PUBLIC_URL=http://192.168.178.100:8881

# Two-way approve button (default true). Set false for notification-only.
TELEGRAM_TWO_WAY=true
```

Then restart:

```bash
docker compose up -d
```

## 4. Test it

- **Quick check:** trigger an import — click **Poll Paperless** (or **Poll
  Lidl**) in the app, or import a new receipt. You should get a message like:

  > 🧾 **New Lidl Hamm** · ready to review
  > 11 items · €37.89
  > _Banane lose, Orangen, …_
  > [ ✅ Approve & split ] [ ✏️ Review ]

- **Two-way check:** press **✅ Approve & split**. The bot creates the Spliit
  expense for the receipt's items and edits the message to
  `✅ Created Spliit expense for €37.89.`

> Notifications only fire for **auto-imported** receipts (webhook / poller), not
> for ones you add by hand in the UI (you're already looking at those).

## What the buttons do

| Button | Action |
|--------|--------|
| **✅ Approve & split** | Creates the Spliit expense for all of the receipt's included items, with your default split (you as payer, even split), and marks it settled. |
| **✏️ Review** | Opens the receipt in the web UI so you can tick/untick or correct items before creating the expense. |

**Safety built in:**
- Only button presses from your `TELEGRAM_CHAT_ID` are honored — anyone else
  gets "Not authorized".
- Approving is **idempotent**: a receipt that's already settled is never charged
  twice, so a stray or repeated tap does nothing.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No messages at all | Confirm `TELEGRAM_ENABLED=true`, the token and chat id are correct, and that you pressed **Start** on the bot. Check `docker logs receipt-importer` for `Telegram notification failed`. |
| Messages arrive, but no buttons | `TELEGRAM_TWO_WAY` is `false`, or `APP_PUBLIC_URL` is unset (the Review link needs it). |
| Buttons do nothing when pressed | The long-poll thread only starts when two-way is enabled and Telegram is configured; check the log for `Telegram two-way bot started`. Also ensure the container has outbound internet. |
| "Not authorized" on press | The press came from a chat id other than `TELEGRAM_CHAT_ID`. Set the correct id. |
| Getting `409 Conflict` in logs | Something else is already polling this bot (e.g. a second instance, or a webhook set on the bot). Run only one instance; if you ever set a webhook, remove it with `https://api.telegram.org/bot<TOKEN>/deleteWebhook`. |

## Security notes

- The bot token grants full control of the bot — store it only in `.env`
  (which is git-ignored), never commit it.
- The service acts on button presses **only** from the single configured chat
  id, so approvals can't be triggered by strangers who find your bot.
