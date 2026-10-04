import os
import asyncio
from dotenv import load_dotenv
from supabase import create_client, Client
from groq import AsyncGroq
import json

load_dotenv()

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(supabase_url, supabase_key)
groq_client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))

async def recategorize_transaction(txn):
    user_id = txn["user_id"]
    description = txn["description"]
    amount = txn["amount"]
    old_category = txn["category"]
    
    try:
        default_res = supabase.table("expense_categories").select("name").is_("user_id", "null").execute()
        default_categories = [c["name"] for c in default_res.data]
        if not default_categories:
            default_categories = [
                "Housing & Rent", "Household Groceries", "Dining, Delivery & Snacks", "Utilities & Bills", "Transport & Travel", "Clothing & Fashion", "Medical & Healthcare", "Grooming & Fitness", "Entertainment & Leisure", "Subscriptions & Software", "Education & Learning", "Finance & Investments", "Gifts & Donations", "Pets & Animals", "Unspecified & Miscellaneous"
            ]
            
        cat_res = supabase.table("expense_categories").select("name").eq("user_id", user_id).execute()
        custom_categories = [c["name"] for c in cat_res.data]
        categories = default_categories + custom_categories
    except:
        categories = [
            "Housing & Rent", "Household Groceries", "Dining, Delivery & Snacks", "Utilities & Bills", "Transport & Travel", "Clothing & Fashion", "Medical & Healthcare", "Grooming & Fitness", "Entertainment & Leisure", "Subscriptions & Software", "Education & Learning", "Finance & Investments", "Gifts & Donations", "Pets & Animals", "Unspecified & Miscellaneous"
        ]

    prompt = f"""
    You are a precise financial categorization AI.
    Analyze the following expense transaction and assign the MOST relevant category from the exact list provided.
    
    Transaction Description: {description}
    Transaction Amount: {amount}
    
    Available Categories: {', '.join(categories)}
    
    Rules:
    - Choose the MOST relevant category from this exact list.
    - Do NOT default to 'Unspecified & Miscellaneous' if a broader category fits. Use 'Unspecified & Miscellaneous' ONLY as a last resort.
    
    Return ONLY a JSON object with a single key "category".
    """
    
    try:
        completion = await groq_client.chat.completions.create(
            messages=[{"role": "system", "content": prompt}],
            model="openai/gpt-oss-120b",
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        ai_response = json.loads(completion.choices[0].message.content)
        new_category = ai_response.get("category", "Unspecified & Miscellaneous")
        
        # Verify the new category is somewhat valid (fallback just in case)
        if new_category not in categories:
            # Maybe the LLM hallucinated, try to find a partial match or default
            matched = False
            for c in categories:
                if c.lower() in new_category.lower() or new_category.lower() in c.lower():
                    new_category = c
                    matched = True
                    break
            if not matched:
                new_category = "Unspecified & Miscellaneous" if "Unspecified & Miscellaneous" in categories else (categories[-1] if categories else "Others")

        # Update if changed
        if new_category != old_category:
            supabase.table("transactions").update({"category": new_category}).eq("id", txn["id"]).execute()
            return True, old_category, new_category
        return False, old_category, new_category
    except Exception as e:
        print(f"Error processing {txn['id']}: {e}")
        return False, old_category, old_category

async def main():
    print("Fetching all expenses...")
    # Using a simple fetch. If there are >1000 rows, pagination is needed.
    # We will loop and paginate.
    limit = 1000
    offset = 0
    all_expenses = []
    
    while True:
        res = supabase.table("transactions").select("*").eq("transaction_type", "expense").range(offset, offset + limit - 1).execute()
        if not res.data:
            break
        all_expenses.extend(res.data)
        if len(res.data) < limit:
            break
        offset += limit

    print(f"Found {len(all_expenses)} expenses. Re-categorizing...")
    
    changed_count = 0
    # Process sequentially to avoid rate limits, though Groq is fast.
    for i, txn in enumerate(all_expenses):
        print(f"Processing {i+1}/{len(all_expenses)}...")
        changed, old, new = await recategorize_transaction(txn)
        if changed:
            changed_count += 1
            print(f"[UPDATED] '{txn['description']}' | {old} -> {new}")
            
    print(f"\nDone! Re-categorized {changed_count} transactions out of {len(all_expenses)}.")

if __name__ == "__main__":
    asyncio.run(main())
