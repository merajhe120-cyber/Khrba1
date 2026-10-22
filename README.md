# KAHRBA Telegram Bot

Arabic Telegram bot built with Python, aiogram 3 and PostgreSQL.

## Features
- One-time welcome bonus (default 25,000 SYP).
- Referral links; referral is rewarded only after new user completes security question and mandatory-channel checks.
- Daily gift-code redemption limit per user, plus one redemption per code per user.
- Mandatory channel management in admin panel.
- Admin-only keyboard button and server-side authorization using `ADMIN_ID`.
- Balance adjustments, gift-code creation, broadcast, bans, logs and statistics.

## Deploy to Render
1. Create a GitHub repository and upload these files.
2. In Render choose **New > Blueprint** and select the repository containing `render.yaml`.
3. Set `BOT_TOKEN` to your Telegram bot token in Render environment variables.
4. Deploy. Render will create the PostgreSQL database and background worker.
5. For every mandatory channel, add the bot as an administrator so Telegram permits membership checks.

## Important notes
- Never commit your real bot token or database URL.
- Telegram reply-keyboard buttons do not support custom background colors; colors depend on the user's Telegram theme.
- Render plan availability and free-tier limits can change. Confirm the current plan in your dashboard.
- Test with a second Telegram account before inviting users.
- The current implementation includes core features, but should be tested in a staging bot before use with real balances.
