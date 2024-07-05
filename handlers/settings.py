from textwrap import dedent
from typing import Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from handlers.expectations import EXPECTING_TOKEN, set_expectation
from lunch import get_lunch_client
from persistence import get_db
from utils import get_chat_id


async def handle_register_token(
    update: Update, context: ContextTypes.DEFAULT_TYPE, token_override: str = None
):
    # if the message is empty, ask to provide a token
    if token_override is None and len(update.message.text.split(" ")) < 2:
        msg = await context.bot.send_message(
            chat_id=update.message.chat_id,
            text="Please provide a token to register",
        )
        set_expectation(
            get_chat_id(update),
            {
                "expectation": EXPECTING_TOKEN,
                "msg_id": msg.message_id,
            },
        )
        return

    if token_override is not None:
        token = token_override
    else:
        token = update.message.text.split(" ")[1]

    # delete the message with the token
    await context.bot.delete_message(
        chat_id=update.message.chat_id, message_id=update.message.message_id
    )

    try:
        # make sure the token is valid
        lunch = get_lunch_client(token)
        lunch_user = lunch.get_user()
        get_db().save_token(update.message.chat_id, token)

        # TODO include basic docs of the available commands
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=dedent(
                f"""
                Hello {lunch_user.user_name}!

                Your token was successfully registered. Will start polling for unreviewed transactions.

                Use /settings to change my behavior.

                (_I deleted token you provided for security purposes_)
                """
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=dedent(
                f"""
                Failed to register token `{token}`:
                ```
                {e}
                ```
                """
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )


def get_current_settings_text(chat_id: int) -> Optional[str]:
    settings = get_db().get_current_settings(chat_id)
    if settings is None:
        return None

    poll_interval = settings["poll_interval_secs"]
    if poll_interval is None or poll_interval == 0:
        poll_interval = "Disabled"
    else:
        if poll_interval < 3600:
            poll_interval = f"`{poll_interval // 60} minutes`"
        elif poll_interval < 86400:
            if poll_interval // 3600 == 1:
                poll_interval = "`1 hour`"
            else:
                poll_interval = f"`{poll_interval // 3600} hours`"
        else:
            if poll_interval // 86400 == 1:
                poll_interval = "`1 day`"
            else:
                poll_interval = f"`{poll_interval // 86400} days`"

    token = settings["token"]

    return dedent(
        f"""
        *Current settings*

        Poll interval: {poll_interval}
        API token: ||{token}||
        """
    )


def get_settings_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Change poll interval",
                    callback_data="changePollInterval",
                ),
                InlineKeyboardButton(
                    "Change token",
                    callback_data="registerToken",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Done",
                    callback_data="doneSettings",
                )
            ],
        ]
    )


async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a message with the current settings."""
    settings_text = get_current_settings_text(update.message.chat_id)
    if settings_text is None:
        await update.message.reply_text(
            text="No settings found for this chat. Did you register a token?",
        )
        return

    await update.message.reply_text(
        text=settings_text,
        reply_markup=get_settings_buttons(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_set_token_from_button(update: Update, _: ContextTypes.DEFAULT_TYPE):
    msg = await update.callback_query.edit_message_text(
        text="Please provide a token to register",
    )
    set_expectation(
        get_chat_id(update),
        {
            "expectation": EXPECTING_TOKEN,
            "msg_id": msg.message_id,
        },
    )


async def handle_change_poll_interval(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Changes the poll interval for the chat."""
    if "_" in update.callback_query.data:
        poll_interval = int(update.callback_query.data.split("_")[1])
        get_db().update_poll_interval(get_chat_id(update), poll_interval)
        await update.callback_query.edit_message_text(
            text=f"_Poll interval updated_\n\n{get_current_settings_text(get_chat_id(update))}",
            reply_markup=get_settings_buttons(),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await update.callback_query.edit_message_text(
            text="Please choose the new poll interval in minutes...",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "5 minutes",
                            callback_data="changePollInterval_300",
                        ),
                        InlineKeyboardButton(
                            "30 minutes",
                            callback_data="changePollInterval_1800",
                        ),
                        InlineKeyboardButton(
                            "1 hour",
                            callback_data="changePollInterval_3600",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "4 hours",
                            callback_data="changePollInterval_14400",
                        ),
                        InlineKeyboardButton(
                            "24 hours",
                            callback_data="changePollInterval_86400",
                        ),
                        InlineKeyboardButton(
                            "Disable",
                            callback_data="changePollInterval_0",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "Cancel",
                            callback_data="cancelPollIntervalChange",
                        )
                    ],
                ]
            ),
        )


async def handle_done_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # delete message
    await context.bot.delete_message(
        chat_id=get_chat_id(update),
        message_id=update.callback_query.message.message_id,
    )
