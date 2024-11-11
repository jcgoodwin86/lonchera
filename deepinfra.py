import os
import random
from dotenv import load_dotenv
from lunchable import TransactionUpdateObject
import requests

from textwrap import dedent
from lunchable.models import (
    TransactionObject,
    CategoriesObject,
)

from lunch import get_lunch_client, get_lunch_client_for_chat_id
from utils import remove_emojis


def get_transaction_input_variable(transaction: TransactionObject) -> str:
    tx_input_variable = dedent(
        f"""
    Payee: {transaction.payee}
    Amount: {transaction.amount} {transaction.currency}"""
    )
    if transaction.plaid_metadata is not None:
        tx_input_variable += dedent(
            f"""
        merchant_name: {transaction.plaid_metadata['merchant_name']}
        name: {transaction.plaid_metadata['name']}"""
        )

    if transaction.notes:
        tx_input_variable += dedent(
            f"""
        notes: {transaction.notes}
        """
        )

    return tx_input_variable


def get_categories_input_variable(categories: list[CategoriesObject]) -> str:
    categories_info = []
    for category in categories:
        # when a category has subcategories (children is not empty),
        # we want to add an item to the categories_info with this format:
        # id: subcategory_name (parent_category_name)
        # but when a category has no subcategories, we want to add an item with this format:
        # id: category_name
        if category.children:
            for subcategory in category.children:
                categories_info.append(
                    f"{subcategory.id}:{remove_emojis(subcategory.name)} ({remove_emojis(category.name)})"
                )
        elif category.group_id is None:
            categories_info.append(f"{category.id}:{remove_emojis(category.name)}")
    return "\n".join(categories_info)


def build_prompt(
    transaction: TransactionObject, categories: list[CategoriesObject]
) -> str:
    print(get_transaction_input_variable(transaction))
    return dedent(
        f"""
This is the transaction information:
{get_transaction_input_variable(transaction)}
What of the following categories would you suggest for this transaction?

Respond with the ID of the category, and only the ID.

These are the available categories (using the format `ID:Category Name`):

{get_categories_input_variable(categories)}
            
Remember to ONLY RESPOND with the ID, and nothing else.
        """
    )


def test_recategorization():
    load_dotenv()
    lunch = get_lunch_client(os.getenv("LUNCH_MONEY_TOKEN"))
    # take 3 random transactions and save them in an array:
    txs = lunch.get_transactions()
    random_txs = random.sample(txs, 3)
    categories = lunch.get_categories()

    for tx in random_txs:
        print("-----------")
        print(tx)
        print()
        prompt = build_prompt(tx, categories)
        category_id = send_message_to_llm(prompt)
        print("suggested category: ", category_id)
        category = [
            category for category in categories if category.id == int(category_id)
        ][0]
        print("category: ", category)


def send_message_to_llm(content):
    url = "https://api.deepinfra.com/v1/openai/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.getenv("DEEPINFRA_API_KEY"),
    }
    data = {
        "model": "meta-llama/Meta-Llama-3.1-405B-Instruct",
        "temperature": 0.0,
        "messages": [{"role": "user", "content": content}],
    }

    response = requests.post(url, headers=headers, json=data)

    if response.status_code == 200:
        return response.json()["choices"][0]["message"]["content"]
    else:
        response.raise_for_status()


# test_recategorization()


def auto_categorize(tx_id: int, chat_id: int) -> str:
    lunch = get_lunch_client_for_chat_id(chat_id)
    tx = lunch.get_transaction(tx_id)
    categories = lunch.get_categories()

    prompt = build_prompt(tx, categories)
    print(prompt)

    try:
        category_id = send_message_to_llm(prompt)
        if int(category_id) == tx.category_id:
            # no need to recategorize
            return "Already categorized correctly"

        for cat in categories:
            if cat.id == int(category_id):
                lunch.update_transaction(
                    tx_id, TransactionUpdateObject(category_id=cat.id)
                )
                return f"Transaction recategorized to {cat.name}"

        return "AI failed to categorize the transaction"
    except Exception as e:
        print(e)
        return "AI crashed while categorizing the transaction"
