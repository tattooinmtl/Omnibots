"""Talking to the team from the phone or any browser (PLAN.md A17.h): Telegram and Discord.

Both connectors only make OUTGOING requests (long polling), so nothing listens on the PC (ADR-2). Only the owner's
own chat is obeyed; everyone else is ignored. Tokens live in the vault (telegram_bot_token, discord_bot_token).
"""
