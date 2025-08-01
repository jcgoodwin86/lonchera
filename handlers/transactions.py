import logging
from datetime import UTC, datetime, timedelta
from textwrap import dedent

from lunchable import TransactionUpdateObject
from lunchable.models import TransactionObject
from telegram import ForceReply
from telegram.constants import ParseMode, ReactionEmoji
from telegram.ext import ContextTypes

from constants import NOTES_MAX_LENGTH
from deepinfra import auto_categorize
from handlers.categorization import ai_categorize_transaction
from handlers.expectations import EDIT_NOTES, RENAME_PAYEE, SET_TAGS, set_expectation
from handlers.lunch_money_agent import handle_generic_message_with_ai
from lunch import get_lunch_client_for_chat_id
from persistence import get_db
from telegram_extensions import Update
from tx_messaging import get_tx_buttons, send_plaid_details, send_transaction_message
from utils import Keyboard, ensure_token

logger = logging.getLogger("tx_handler")


# Sort transactions by date in chronological order (oldest first)
# Use plaid's authorized_datetime if available for more precise sorting
def get_transaction_datetime(t):
    if t.plaid_metadata and "authorized_datetime" in t.plaid_metadata and t.plaid_metadata["authorized_datetime"]:
        return datetime.fromisoformat(t.plaid_metadata["authorized_datetime"])
    return datetime.combine(t.date, datetime.min.time()).replace(tzinfo=UTC)


async def fetch_transactions(chat_id: int, days_lookback: int, pending: bool):
    """
    Fetch transactions from LunchMoney API.

    Args:
        chat_id: Chat ID to get LunchMoney client for
        days_lookback: Number of days to look back from today
        pending: If True, fetch pending transactions. If False, fetch posted transactions.

    Returns:
        List of transactions sorted chronologically
    """
    lunch = get_lunch_client_for_chat_id(chat_id)
    start_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_lookback)
    end_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    transactions = lunch.get_transactions(pending=pending, start_date=start_date, end_date=end_date)

    # TODO: this seems to be a bug in the LunchMoney API
    # Filter out transactions whose pending state does not match the requested one
    transactions = [tx for tx in transactions if tx.is_pending == pending]

    logger.info(f"Found {len(transactions)} {'pending' if pending else 'posted'} transactions for chat {chat_id}")

    # Sort transactions by date in chronological order (oldest first)
    try:
        transactions.sort(key=get_transaction_datetime)
    except Exception:
        logger.exception("Error sorting transactions")

    return transactions


async def check_transactions_and_telegram_them(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, poll_pending: bool = False
):
    """
    Main function to check for new transactions and send them to Telegram.

    This function orchestrates the entire transaction processing workflow:
    1. Fetches transactions from LunchMoney API (pending or posted)
    2. Handles transaction ID updates and auto-review based on settings
    3. Sends new transactions to Telegram
    4. Updates existing Telegram messages for pending transactions

    Args:
        context: Telegram context
        chat_id: Chat ID to send messages to
        poll_pending: If True, fetch pending transactions. If False, fetch posted transactions.
    """
    ## 1. Fetch transactions from LunchMoney API
    days_lookback = 15
    logger.info(
        f"Polling for {'pending' if poll_pending else 'posted'} transactions from {days_lookback} days ago for chat {chat_id}..."
    )

    # Always get posted transactions
    posted_transactions = await fetch_transactions(chat_id, days_lookback, pending=False)

    # Get pending transactions if requested
    if poll_pending:
        pending_transactions = await fetch_transactions(chat_id, days_lookback, pending=True)
        transactions_to_process = pending_transactions + posted_transactions
    else:
        transactions_to_process = posted_transactions

    settings = get_db().get_current_settings(chat_id)
    all_updated_message_ids = set()

    ## 2. Handle transaction ID updates and auto-review based on settings
    if poll_pending:
        # Update transaction IDs for transactions that changed from pending to posted
        id_update_message_ids = await update_transaction_ids_for_posted_transactions(chat_id, posted_transactions)

        # Mark posted transactions as reviewed if auto_mark_reviewed is enabled
        if settings.auto_mark_reviewed:
            # Filter to only unreviewed transactions to avoid unnecessary processing
            unreviewed_posted_transactions = [tx for tx in posted_transactions if tx.status == "uncleared"]
            reviewed_message_ids = await mark_posted_txs_as_reviewed(context, chat_id, unreviewed_posted_transactions)
        else:
            reviewed_message_ids = []

        all_updated_message_ids = set(id_update_message_ids + reviewed_message_ids)
    else:
        # Handle auto-review for posted transactions
        lunch = get_lunch_client_for_chat_id(chat_id)
        for transaction in transactions_to_process:
            # Auto-mark as reviewed for posted transactions if enabled
            if settings.auto_mark_reviewed and transaction.status == "uncleared":
                lunch.update_transaction(
                    transaction.id,
                    TransactionUpdateObject(status=TransactionUpdateObject.StatusEnum.cleared),  # type: ignore
                )
                transaction.status = "cleared"

    # 3. Send new transactions to Telegram that haven't been sent before
    for transaction in transactions_to_process:
        if get_db().was_already_sent(transaction.id):
            logger.debug(f"Skipping already sent transaction {transaction.id} in chat {chat_id}")
            continue

        msg_id = await send_transaction_message(context, transaction, chat_id)
        get_db().mark_as_sent(
            transaction.id,
            chat_id,
            msg_id,
            transaction.recurring_type,
            reviewed=transaction.status == "cleared",
            plaid_id=(transaction.plaid_metadata.get("transaction_id", None) if transaction.plaid_metadata else None),
        )

    # 4. Update Telegram messages for transactions that had their IDs updated or were marked as reviewed
    if poll_pending:
        await resync_updted_transactions(context, chat_id, all_updated_message_ids)


async def resync_updted_transactions(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, updated_message_ids: set[int]
) -> None:
    """Handle updating Telegram messages for transactions that had their IDs updated or were marked as reviewed."""
    if not updated_message_ids:
        return

    logger.info(f"Resyncing {len(updated_message_ids)} updated transactions for chat {chat_id}")
    lunch = get_lunch_client_for_chat_id(chat_id)
    for message_id in updated_message_ids:
        try:
            # Get the transaction ID associated with this message
            tx_id = get_db().get_tx_associated_with(message_id, chat_id)
            if tx_id:
                # Get the updated transaction data from LunchMoney
                updated_tx = lunch.get_transaction(tx_id)
                if updated_tx:
                    # Update the Telegram message with the latest transaction data
                    await send_transaction_message(
                        context, transaction=updated_tx, chat_id=chat_id, message_id=message_id
                    )
                    logger.info(f"Updated Telegram message {message_id} for transaction {tx_id} in chat {chat_id}")
                else:
                    logger.warning(f"Could not retrieve transaction data for transaction {tx_id} in chat {chat_id}")
            else:
                logger.warning(f"Could not find transaction ID for message {message_id} in chat {chat_id}")
        except Exception:
            logger.exception(f"Failed to update Telegram message {message_id} in chat {chat_id}")


async def update_transaction_ids_for_posted_transactions(
    chat_id: int, posted_transactions: list[TransactionObject]
) -> list[int]:
    """Update transaction records when pending transactions become posted and their IDs change.

    This function is necessary to solve a critical issue with transaction ID management:

    PROBLEM:
    - When the bot polls for pending transactions, it sends Telegram messages and stores
      the transaction records in the database with the current transaction ID
    - However, when a pending transaction becomes posted (cleared), the transaction ID
      in LunchMoney often changes to a new value
    - This leaves the Telegram message associated with the wrong transaction ID in our database
    - Without this fix, operations like marking transactions as reviewed, categorizing, etc.
      would fail because they can't find the correct transaction record

    SOLUTION:
    - Use the plaid_metadata.pending_transaction_id field to link old and new transaction IDs
    - When a transaction was pending, its plaid_id is stored in our database
    - When the same transaction becomes posted, plaid_metadata.pending_transaction_id
      contains the original plaid_id (the one we stored)
    - We match these values to identify which database records need updating
    - Update both tx_id (LunchMoney ID) and plaid_id (current Plaid ID) in our database

    FLOW:
    1. Get all previously sent pending transactions for this chat
    2. Create a mapping of pending_transaction_id → posted transaction
    3. For each sent pending transaction, check if its plaid_id matches any
       posted transaction's pending_transaction_id
    4. If match found, update the database record with the new IDs

    This ensures Telegram messages remain correctly linked to their transactions
    even after the transaction IDs change during the pending → posted transition.

    Returns:
        list[int]: List of Telegram message IDs that were updated
    """
    logger.info(f"Checking for transaction ID changes for chat {chat_id}...")
    updated_message_ids = []

    # Get all previously sent pending transactions for this chat
    two_weeks_ago = datetime.now() - timedelta(days=14)
    sent_txs = get_db().get_sent_transactions(chat_id, since=two_weeks_ago) or []

    if not sent_txs:
        logger.info(f"No sent transactions found for chat {chat_id} in the last two weeks")
        return updated_message_ids

    logger.info(f"Found {len(sent_txs)} sent transactions to check in chat {chat_id}")

    # Create a mapping of pending_transaction_id to posted transaction for efficient lookup
    posted_by_pending_id = {}
    for posted_tx in posted_transactions:
        if posted_tx.plaid_metadata and posted_tx.plaid_metadata.get("pending_transaction_id"):
            pending_id = posted_tx.plaid_metadata["pending_transaction_id"]
            posted_by_pending_id[pending_id] = posted_tx

    logger.info(f"Found {len(posted_by_pending_id)} posted transactions with pending_transaction_id in chat {chat_id}")

    # Check each sent pending transaction to see if it needs to be updated
    for sent_tx in sent_txs:
        if not sent_tx.plaid_id:
            logger.debug(f"Skipping sent pending transaction {sent_tx.tx_id} without plaid_id in chat {chat_id}")
            continue

        # Look for a posted transaction that has this transaction's plaid_id as pending_transaction_id
        if sent_tx.plaid_id in posted_by_pending_id:
            posted_tx = posted_by_pending_id[sent_tx.plaid_id]
            new_plaid_id = posted_tx.plaid_metadata.get("transaction_id") if posted_tx.plaid_metadata else None

            logger.info(
                f"Found ID change: pending tx {sent_tx.tx_id} (plaid_id: {sent_tx.plaid_id}) "
                f"-> posted tx {posted_tx.id} (plaid_id: {new_plaid_id})"
            )

            # Update the transaction record with the new IDs
            success = get_db().update_transaction_ids_by_plaid_id(
                old_plaid_id=sent_tx.plaid_id, new_tx_id=posted_tx.id, new_plaid_id=new_plaid_id
            )

            if success:
                logger.info(f"Successfully updated transaction record for {sent_tx.plaid_id}")
                updated_message_ids.append(sent_tx.message_id)
            else:
                logger.warning(f"Failed to update transaction record for {sent_tx.plaid_id}")

    return updated_message_ids


async def mark_posted_txs_as_reviewed(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, posted_transactions: list[TransactionObject]
) -> list[int]:
    """Mark previously sent pending transactions as reviewed if they are now posted.

    Args:
        context: Telegram context for sending messages
        chat_id: Chat ID to process transactions for
        posted_transactions: List of unreviewed posted transactions to check against

    Returns:
        list[int]: List of Telegram message IDs that were updated for reviewed transactions
    """
    lunch = get_lunch_client_for_chat_id(chat_id)
    logger.info(f"Checking if any previously sent transactions are now posted for {chat_id}...")
    updated_message_ids = []

    # Get all previously sent pending transactions
    two_weeks_ago = datetime.now() - timedelta(days=14)
    sent_txs = get_db().get_sent_transactions(chat_id, since=two_weeks_ago) or []
    if not sent_txs:
        logger.info(f"No sent transactions found for chat {chat_id} in the last two weeks")
        return []

    logger.info(f"Found {len(sent_txs)} previously sent transactions for {chat_id}")

    # Print basic info for each posted transaction
    for tx in posted_transactions:
        logger.debug(
            f"Posted Transaction: ID={tx.id}, Date={tx.date}, Amount={tx.amount}, Status={tx.status}, Payee={tx.payee}"
        )

    # Create lookup dictionaries for efficient matching
    posted_by_id = {int(tx.id): tx for tx in posted_transactions}

    # Create a map of still pending "sent tx messages" to posted_tx object
    tx_to_process = {}
    for sent_tx in sent_txs:
        posted_tx = None

        # First try to match by transaction ID
        if sent_tx.tx_id in posted_by_id:
            posted_tx = posted_by_id[sent_tx.tx_id]
        else:
            continue

        if not posted_tx:  # TODO: this should also check for pending but not now because of a previous bug
            continue

        tx_to_process[sent_tx] = posted_tx

    logger.info(f"Found {len(tx_to_process)} transactions to mark as reviewed in chat {chat_id}")

    # Check each sent transaction
    for sent_tx, posted_tx in tx_to_process.items():
        # Mark the found transaction as reviewed (transactions are pre-filtered to uncleared only)
        logger.info(
            f"Checking sent transaction {sent_tx.id} against posted transaction {posted_tx.id} with status {posted_tx.status}"
        )
        logger.info(f"Marking previously sent transaction {posted_tx.id} as reviewed")
        try:
            lunch.update_transaction(
                posted_tx.id,
                TransactionUpdateObject(status=TransactionUpdateObject.StatusEnum.cleared),  # type: ignore
            )

            # Also mark as reviewed in the db
            get_db().mark_as_reviewed_by_tx_id(posted_tx.id, chat_id)

            msg_id = get_db().get_message_id_associated_with(posted_tx.id, chat_id)
            if msg_id:
                updated_message_ids.append(msg_id)
        except Exception:
            logger.exception(f"Failed to mark transaction {posted_tx.id} as reviewed")

    if not updated_message_ids:
        logger.info(f"No transactions were updated for chat {chat_id}")

    return updated_message_ids


async def handle_check_transactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        settings = ensure_token(update)
        await check_transactions_and_telegram_them(context, chat_id=update.chat_id, poll_pending=settings.poll_pending)
        get_db().update_last_poll_at(update.chat_id, datetime.now().isoformat())
    except Exception:
        logger.exception(f"Failed to check transactions for chat {update.chat_id}")


async def handle_btn_skip_transaction(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.safe_edit_message_reply_markup(
        reply_markup=None,
        answer_text="Transaction was left intact. You must review it manually from lunchmoney.app",
        show_alert=True,
    )


async def handle_btn_collapse_transaction(update: Update, _: ContextTypes.DEFAULT_TYPE):
    tx_id = int(update.callback_data_suffix)
    await update.safe_edit_message_reply_markup(reply_markup=get_tx_buttons(update.chat_id, tx_id, collapsed=True))


async def handle_btn_cancel_categorization(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if update.callback_query is None:
        return

    tx_id = int(update.callback_data_suffix)
    await update.safe_edit_message_reply_markup(reply_markup=get_tx_buttons(update.chat_id, tx_id))


async def handle_btn_show_categories(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Updates the message to show the parent categories available"""
    if update.callback_query is None:
        return

    chat_id = update.chat_id
    lunch = get_lunch_client_for_chat_id(chat_id)
    transaction_id = int(update.callback_data_suffix)

    categories = lunch.get_categories()
    kbd = Keyboard()
    for category in categories:
        if category.group_id is None:
            if category.children:
                kbd += (f"📂 {category.name}", f"subcategorize_{transaction_id}_{category.id}")
            else:
                kbd += (category.name, f"applyCategory_{transaction_id}_{category.id}")

    kbd += ("Cancel", f"cancelCategorization_{transaction_id}")

    await update.safe_edit_message_reply_markup(reply_markup=kbd.build(columns=2))


async def handle_btn_show_subcategories(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Updates the transaction with the selected category."""
    if update.callback_query is None:
        return

    query = update.callback_query
    if query.data is None or query.message is None or query.message.chat is None:
        return

    transaction_id, category_id = query.data.split("_")[1:]

    chat_id = update.chat_id
    lunch = get_lunch_client_for_chat_id(chat_id)
    subcategories = lunch.get_categories()
    kbd = Keyboard()
    for subcategory in subcategories:
        if str(subcategory.group_id) == str(category_id):
            kbd += (subcategory.name, f"applyCategory_{transaction_id}_{subcategory.id}")
    kbd += ("Cancel", f"cancelCategorization_{transaction_id}")

    await update.safe_edit_message_reply_markup(reply_markup=kbd.build(columns=2))


async def handle_btn_apply_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Updates the transaction with the selected category."""
    if update.callback_query is None:
        return

    query = update.callback_query
    if query.message is None or query.message.chat is None or query.data is None:
        return

    chat_id = update.chat_id

    transaction_id, category_id = query.data.split("_")[1:]
    transaction_id = int(transaction_id)
    lunch = get_lunch_client_for_chat_id(chat_id)

    settings = get_db().get_current_settings(chat_id)
    if settings.mark_reviewed_after_categorized:
        update_obj = TransactionUpdateObject(category_id=category_id, status=TransactionUpdateObject.StatusEnum.cleared)  # type: ignore
        lunch.update_transaction(transaction_id, update_obj)
        get_db().mark_as_reviewed(query.message.message_id, chat_id)
    else:
        update_obj = TransactionUpdateObject(category_id=category_id)  # type: ignore
        lunch.update_transaction(transaction_id, update_obj)
    logger.info(f"Changed category for tx {transaction_id} to {category_id}")

    updated_transaction = lunch.get_transaction(transaction_id)
    await send_transaction_message(context, updated_transaction, chat_id, query.message.message_id)
    await query.answer()


async def handle_btn_dump_plaid_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a new message with the plaid metadata of the transaction."""
    transaction_id = int(update.callback_data_suffix)

    chat_id = update.chat_id
    lunch = get_lunch_client_for_chat_id(chat_id)

    transaction = lunch.get_transaction(transaction_id)
    plaid_metadata = transaction.plaid_metadata
    plaid_details = "*Plaid Metadata*\n\n"
    plaid_details += f"*Transaction ID:* {transaction_id}\n"
    if plaid_metadata:
        for key, value in plaid_metadata.items():
            if value is not None:
                plaid_details += f"*{key}:* `{value}`\n"

    await send_plaid_details(update, context, chat_id, transaction_id, plaid_details)


async def handle_btn_mark_tx_as_reviewed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Updates the transaction status to reviewed."""
    query = update.callback_query
    if query is None or query.message is None:
        return
    chat_id = update.chat_id
    lunch = get_lunch_client_for_chat_id(chat_id)
    transaction_id = int(update.callback_data_suffix)
    try:
        lunch.update_transaction(transaction_id, TransactionUpdateObject(status="cleared"))  # type: ignore

        # update message to show the right buttons
        updated_tx = lunch.get_transaction(transaction_id)
        msg_id = get_db().get_message_id_associated_with(transaction_id, chat_id)
        await send_transaction_message(context, transaction=updated_tx, chat_id=chat_id, message_id=msg_id)

        get_db().mark_as_reviewed(query.message.message_id, chat_id)
        await query.answer()
    except Exception as e:
        await query.answer(text=f"Error marking transaction as reviewed: {e!s}", show_alert=True)


async def handle_btn_mark_tx_as_unreviewed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Updates the transaction status to unreviewed."""
    query = update.callback_query
    if query is None or query.message is None:
        return

    chat_id = update.chat_id
    lunch = get_lunch_client_for_chat_id(chat_id)
    transaction_id = int(update.callback_data_suffix)
    try:
        logger.info(f"Marking transaction {transaction_id} as unreviewed")
        lunch.update_transaction(
            transaction_id,
            TransactionUpdateObject(status=TransactionUpdateObject.StatusEnum.uncleared),  # type: ignore
        )

        # update message to show the right buttons
        updated_tx = lunch.get_transaction(transaction_id)
        msg_id = get_db().get_message_id_associated_with(transaction_id, chat_id)
        await send_transaction_message(context, transaction=updated_tx, chat_id=chat_id, message_id=msg_id)

        get_db().mark_as_unreviewed(query.message.message_id, chat_id)
        await query.answer()
    except Exception as e:
        await query.answer(text=f"Error marking transaction as reviewed: {e!s}", show_alert=True)


async def handle_message_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Updates the transaction notes."""
    if update.message is None:
        return None

    chat_id = update.chat_id

    settings = get_db().get_current_settings(chat_id)
    if settings is not None and settings.ai_agent:
        # If AI Agent is enabled, we just pass the message to the AI handler
        return await handle_generic_message_with_ai(update, context)

    replying_to_msg_id = update.message.reply_to_message.message_id if update.message.reply_to_message else -1
    tx_id = get_db().get_tx_associated_with(replying_to_msg_id, chat_id)

    if tx_id is None:
        logger.error("No transaction ID found in bot data", exc_info=True)
        await context.bot.send_message(
            chat_id=chat_id,
            text=dedent(
                """
                Could not find the transaction associated with the message.
                This is a bug if you have not wiped the db.
                """
            ),
        )
        return

    msg_text = update.message.text or ""
    message_are_tags = True
    for word in msg_text.split(" "):
        if not word.startswith("#"):
            message_are_tags = False
            break

    lunch = get_lunch_client_for_chat_id(chat_id)
    if message_are_tags:
        tags_without_hashtag = [tag[1:] for tag in msg_text.split(" ") if tag.startswith("#")]
        logger.info(f"Setting tags to transaction ({tx_id}): {tags_without_hashtag}")
        lunch.update_transaction(tx_id, TransactionUpdateObject(tags=tags_without_hashtag))  # type: ignore
    else:
        notes = msg_text
        if len(notes) > NOTES_MAX_LENGTH:
            notes = notes[:NOTES_MAX_LENGTH]
        logger.info(f"Setting notes to transaction ({tx_id}): {notes}")
        lunch.update_transaction(tx_id, TransactionUpdateObject(notes=notes))  # type: ignore

    # update the transaction message to show the new notes
    updated_tx = lunch.get_transaction(tx_id)
    await send_transaction_message(context, transaction=updated_tx, chat_id=chat_id, message_id=replying_to_msg_id)

    settings = get_db().get_current_settings(chat_id)
    if settings.auto_categorize_after_notes and not message_are_tags:
        await ai_categorize_transaction(tx_id, chat_id, context)

    await context.bot.set_message_reaction(
        chat_id=chat_id, message_id=update.message.message_id, reaction=ReactionEmoji.WRITING_HAND
    )


async def handle_btn_ai_categorize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tx_id = int(update.callback_data_suffix)

    chat_id = update.chat_id
    response = auto_categorize(tx_id, chat_id)
    if update.callback_query:
        await update.callback_query.answer(text=response, show_alert=True)

    # update the transaction message to show the new notes
    lunch = get_lunch_client_for_chat_id(chat_id)
    updated_tx = lunch.get_transaction(tx_id)
    await send_transaction_message(context, transaction=updated_tx, chat_id=chat_id, message_id=update.message_id)


async def poll_transactions_on_schedule(context: ContextTypes.DEFAULT_TYPE):
    """
    Gets called every minute to poll transactions for all registered chats.
    However, each chat can have its own polling settings, so we use this
    function to check the settings for each chat and decide whether to poll.
    """
    chat_ids = get_db().get_all_registered_chats()
    if len(chat_ids) is None:
        logger.warning("No chats registered yet")

    for chat_id in chat_ids:
        settings = get_db().get_current_settings(chat_id)
        if not settings:
            # technically this should never happen, but just in case
            logger.error(f"No settings found for chat {chat_id}!")
            continue

        if settings.token == "revoked":
            logger.debug(f"Skipping chat {chat_id} because API token was revoked.")
            continue

        # this is the last time we polled, saved as a string using:
        # datetime.now().isoformat()
        last_poll_at = settings.last_poll_at
        should_poll = False
        if last_poll_at is None:
            logger.info(f"First poll for chat {chat_id}")
            last_poll_at = datetime.now() - timedelta(days=1)
            should_poll = True
        else:
            poll_interval_seconds = settings.poll_interval_secs
            next_poll_at = last_poll_at + timedelta(seconds=poll_interval_seconds)
            should_poll = datetime.now() >= next_poll_at

        if should_poll:
            try:
                await check_transactions_and_telegram_them(context, chat_id=chat_id, poll_pending=settings.poll_pending)
            except Exception as e:
                # check if the error message is lunchable.exceptions.LunchMoneyHTTPError
                # and the message is: Access token does not exist, which means the user
                # has revoked the access to the app.
                # If that is the case, we should set the API token to 'revoked'.
                if "Access token does not exist" in str(e):
                    get_db().set_api_token(chat_id, "revoked")
                    logger.exception(
                        f"User in chat {chat_id} has revoked access to the app. Setting API token to None."
                    )
            get_db().update_last_poll_at(chat_id, datetime.now().isoformat())


async def handle_expand_tx_options(update: Update, _: ContextTypes.DEFAULT_TYPE):
    tx_id = int(update.callback_data_suffix)
    await update.safe_edit_message_reply_markup(reply_markup=get_tx_buttons(update.chat_id, tx_id, collapsed=False))


async def handle_rename_payee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or not update.callback_query.message:
        return

    transaction_id = int(update.callback_data_suffix)
    await update.callback_query.answer()
    await context.bot.send_message(
        chat_id=update.chat_id,
        text="Please enter the new payee name:",
        reply_to_message_id=update.callback_query.message.message_id,
        reply_markup=ForceReply(),
    )
    set_expectation(
        update.chat_id,
        {
            "expectation": RENAME_PAYEE,
            "msg_id": str(update.callback_query.message.message_id),
            "transaction_id": str(transaction_id),
        },
    )


async def handle_edit_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or not update.callback_query.message:
        return

    transaction_id = int(update.callback_data_suffix)
    await update.callback_query.answer()
    await context.bot.send_message(
        chat_id=update.chat_id,
        text=dedent(
            """
            Please enter notes for this transaction.\n\n
            *Hint:* _you can also reply to the transaction message to edit its notes._"""
        ),
        reply_to_message_id=update.callback_query.message.message_id,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ForceReply(),
    )
    set_expectation(
        update.chat_id,
        {
            "expectation": EDIT_NOTES,
            "msg_id": str(update.callback_query.message.message_id),
            "transaction_id": str(transaction_id),
        },
    )


async def handle_set_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query or not update.callback_query.message:
        return

    transaction_id = int(update.callback_data_suffix)
    await update.callback_query.answer()
    await context.bot.send_message(
        chat_id=update.chat_id,
        text=dedent(
            """
            Please enter the tags for this transaction\n\n
            💡 *Hint:* _you can also reply to the transaction message to edit its tags
            if the message contains only tags like this: #tag1 #tag2 #etc_
            """
        ),
        reply_to_message_id=update.callback_query.message.message_id,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ForceReply(),
    )
    set_expectation(
        update.chat_id,
        {
            "expectation": SET_TAGS,
            "msg_id": str(update.callback_query.message.message_id),
            "transaction_id": str(transaction_id),
        },
    )


async def handle_btn_close_plaid_details(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Deletes the plaid metadata message."""
    await update.safe_delete_message()
