# PS D:\PROJECTS\MemApp\Backend> curl.exe -X POST "http://127.0.0.1:8000/api/internal/check-reminders" -H "Authorization: Bearer api_key"


import os
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from groq import AsyncGroq
from dotenv import load_dotenv
from supabase import create_client, Client 
from google import genai
from google.genai import types
import json
from fastapi import Header
from datetime import datetime, timedelta, timezone
import resend

# Load environment variables from .env
load_dotenv()

app = FastAPI()

# Enable CORS for browser communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize API Clients
groq_client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))
resend.api_key = os.environ.get("RESEND_API_KEY")
genai_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")
supabase_client: Client = create_client(supabase_url, supabase_key)



async def extract_reminders(text: str, memory_id: str):
    # Give the LLM the current date/time context (Nepal Standard Time)
    current_time = datetime.now().strftime("%A, %B %d, %Y %I:%M %p")
    
    system_prompt = f"""
    You are a precise calendar extraction AI. The current date and time is {current_time}.
    Analyze the user's memory and extract any explicit or implied tasks, meetings, or deadlines.
    Return ONLY a raw JSON array of objects. Do not include markdown formatting, backticks, or conversational text.
    Each object must have exactly two keys: 
    - "task_name": A short, clear string.
    - "due_datetime": An ISO 8601 formatted timestamp (YYYY-MM-DDTHH:MM:SS+05:45).
    If no events are mentioned, return an empty array [].
    """

    try:
        completion = await groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            model="llama-3.1-8b-instant",
            temperature=0, 
        )
        
        response_text = completion.choices[0].message.content.strip()
        # print(f"LLM Raw Output: {response_text}") # Debug print
        
        # Parse the JSON and save to the new database table
        reminders = json.loads(response_text)
        
        for reminder in reminders:
            supabase_client.table("reminders").insert({
                "memory_id": memory_id,
                "task_name": reminder["task_name"],
                "due_datetime": reminder["due_datetime"]
            }).execute()
            
        return len(reminders)
        
    except Exception as e:
        print(f"Extraction skipped or failed: {e}")
        return 0

async def extract_transactions(text: str, memory_id: str):
    system_prompt = """
    You are a precise financial extraction AI. Analyze the user's memory for any debts, loans, payments, transactions or shared expenses.
    Return ONLY a raw JSON array of objects. Do not include markdown formatting or backticks.
    
    CRITICAL RULES:
    1. EXCLUDE SELF-PURCHASES: Ignore money the user spent entirely on themselves. NEVER use "I", "Me", "My", or "You" as a person_name.
    2. THE MATH RULE: If multiple items are bought, find the exact UNIT PRICE. Multiply that unit price by the quantity given to each person.
    3. GOODS = MONEY: If the user buys physical items and gives them to someone else, calculate the monetary value of those items. That person now owes the user. If monetary value of the items is not explicitly stated, return that monetary value is missing and skip that transaction. Do not attempt to guess or estimate the value.
    4. Calculate the total net amount per person mentioned.
    5. Output ONLY ONE JSON object per person per event. NEVER output duplicate objects. 

    Each object must have exactly these keys in this exact order:
    - "reasoning": Briefly state who paid and the math, conclude with e.g. "[Name] bought 2 bags * 1300 unit price = 2600 for User -> THEY" OR "User bought 2 bags * 1300 unit price = 2600 for [Name] -> USER". You MUST end this sentence with exactly the ALL-CAPS WORD "USER" or "THEY". Do not use the person's name in this final phrase.
    - "person_name": The name of the other person involved.
    - "transaction_type": 
        - If your reasoning ends with "THEY", output EXACTLY "owed_to_them"
        - If your reasoning ends with "USER", output EXACTLY "owed_to_me"
    - "amount": The final calculated numerical amount as a float.

    If no financial transactions are mentioned, return an empty array [].
    """

    try:
        completion = await groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            model="llama-3.1-8b-instant",
            temperature=0, 
        )
        
        response_text = completion.choices[0].message.content.strip()
        print(f"Ledger Extraction: {response_text}")
        
        transactions = json.loads(response_text)
        
        # --- DEFENSIVE FILTER: Remove exact duplicates ---
        seen = set()
        unique_transactions = []
        for txn in transactions:
            # Create a unique fingerprint for each transaction
            fingerprint = (txn.get("person_name", "").lower(), txn.get("amount"), txn.get("transaction_type"))
            if fingerprint not in seen:
                seen.add(fingerprint)
                unique_transactions.append(txn)
        # -------------------------------------------------

        for txn in unique_transactions:
            supabase_client.table("transactions").insert({
                "memory_id": memory_id,
                "person_name": txn["person_name"].lower(), # Lowercase for easy searching
                "amount": float(txn["amount"]),
                "transaction_type": txn["transaction_type"]
            }).execute()
            
        return len(unique_transactions)
        
    except Exception as e:
        print(f"Transaction extraction failed: {e}")
        return 0

@app.post("/api/memory")
async def transcribe_and_store_audio(file: UploadFile = File(...)):
    # Validate allowed formats
    if not file.filename.endswith(('.wav', '.m4a', '.mp3', '.ogg', '.webm')):
        raise HTTPException(status_code=400, detail="Unsupported audio format.")
        
    try:
        # 1. Transcribe via Groq Whisper
        audio_bytes = await file.read()
        transcription = await groq_client.audio.transcriptions.create(
            file=(file.filename, audio_bytes),
            model="whisper-large-v3",
            response_format="text",
            language="en"
        )
        
        raw_text = transcription.strip()
        if not raw_text:
            return {"status": "skipped", "message": "No speech detected in audio clip."}

        # 2. Generate vector embedding locally (768 dimensions)
        # We run this inside the standard runtime; it takes less than a second
        # text_embedding = embedding_model.encode(raw_text).tolist()

        # Generate vector embedding via the new Gemini SDK
        embedding_result = genai_client.models.embed_content(
            model="gemini-embedding-001",
            contents=raw_text,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=768 # Matches your Supabase table perfectly
            )
        )
        
        # The new SDK returns an object, not a dictionary
        text_embedding = embedding_result.embeddings[0].values

        # 3. Store into Supabase Table
        data = {
            "raw_text": raw_text,
            "embedding": text_embedding
        }
        
        response = supabase_client.table("memories").insert(data).execute()

        memory_id = response.data[0]["id"]

        # Run the LLM extraction in the background
        tasks_found = await extract_reminders(raw_text, memory_id)
        txns_found = await extract_transactions(raw_text, memory_id)
        
        return {
            "status": "success",
            "transcription": raw_text,
            "database_id": memory_id,
            "tasks_scheduled": tasks_found
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server Pipeline Error: {str(e)}")


@app.get("/api/search")
async def search_memories(q: str):
    if not q.strip():
        raise HTTPException(status_code=400, detail="Search query cannot be empty.")
        
    try:
        # 1. Convert the plain-text search query into a vector match string
        # query_vector = embedding_model.encode(q.strip()).tolist()

        # Convert search query to vector
        query_result = genai_client.models.embed_content(
            model="gemini-embedding-001",
            contents=q.strip(),
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=768
            )
        )
        
        query_vector = query_result.embeddings[0].values
        
        # 2. Match the vector against your database using the RPC function we just saved
        response = supabase_client.rpc(
            "match_memories",
            {
                "query_embedding": query_vector,
                "match_threshold": 0.2,  # Captures relevant matches
                "match_count": 3         # Return the top 3 most relevant items
            }
        ).execute()
        
        return {
            "status": "success",
            "query": q,
            "results": response.data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search Engine Error: {str(e)}")

@app.post("/api/internal/check-reminders")
async def check_and_send_reminders(authorization: str = Header(None)):
    # 1. Security Check: Ensure only your authorized cron job can trigger this
    expected_secret = os.environ.get("CRON_SECRET")
    if authorization != f"Bearer {expected_secret}":
        raise HTTPException(status_code=401, detail="Unauthorized cron trigger")

    try:
        # 2. Define the time window (e.g., look for tasks due in the next 60 minutes)
        # Using UTC for safe database comparison
        now_utc = datetime.now(timezone.utc)
        time_window = now_utc + timedelta(minutes=60)
        
        # 3. Query Supabase for pending reminders within this window
        response = supabase_client.table("reminders") \
            .select("*") \
            .eq("status", "pending") \
            .lte("due_datetime", time_window.isoformat()) \
            .execute()
            
        due_tasks = response.data
        
        if not due_tasks:
            return {"status": "success", "message": "No pending tasks in the upcoming window."}

        sent_count = 0
        for task in due_tasks:
            
            # Format the time nicely for the email
            # Converting the UTC ISO string back to a readable format
            dt_obj = datetime.fromisoformat(task['due_datetime'].replace('Z', '+00:00'))
            readable_time = dt_obj.strftime("%B %d, %Y at %I:%M %p (UTC)")

            # Send the email via Resend
            try:
                email_response = resend.Emails.send({
                    "from": "MemApp AI <onboarding@resend.dev>", # Resend's free testing address
                    "to": "amsubelbs@gmail.com",       # <--- CHANGE THIS TO YOUR EMAIL
                    "subject": f"Reminder: {task['task_name']}",
                    "html": f"""
                    <div style="font-family: sans-serif; padding: 20px;">
                        <h2>🔔 Upcoming Memory Reminder</h2>
                        <p><strong>Task:</strong> {task['task_name']}</p>
                        <p><strong>Due:</strong> {readable_time}</p>
                        <hr>
                        <p style="color: gray; font-size: 12px;">Sent automatically by your Voice Memory Hub</p>
                    </div>
                    """
                })
                print(f"Email sent successfully! ID: {email_response['id']}")
            except Exception as email_err:
                print(f"Failed to send email: {email_err}")

            # Mark the task as 'sent'
            supabase_client.table("reminders") \
                .update({"status": "sent"}) \
                .eq("id", task["id"]) \
                .execute()
                
            sent_count += 1
            
        return {"status": "success", "notifications_sent": sent_count}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cron Engine Error: {str(e)}")

@app.get("/api/chat")
async def chat_with_memories(q: str):
    if not q:
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required")

    try:
        # 1. Fetch semantic context (Vector Search)
        query_result = genai_client.models.embed_content(
            model="gemini-embedding-001",
            contents=q.strip(),
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=768
            )
        )
        query_vector = query_result.embeddings[0].values

        response = supabase_client.rpc(
            "match_memories",
            {"query_embedding": query_vector, "match_threshold": 0.2, "match_count": 10}
        ).execute()
        
        context_text = "\n".join([f"Memory: {note['raw_text']}" for note in response.data])

        # 2. Fetch the Hard Ledger (SQL Math)
        # We grab all transactions to calculate the exact, unarguable balance sheet
        ledger_response = supabase_client.table("transactions").select("*").execute()
        balances = {}
        
        for txn in ledger_response.data:
            person = txn["person_name"].capitalize()
            if person not in balances:
                balances[person] = 0
            
            # The bulletproof math layer
            if txn["transaction_type"] == "owed_to_me":
                balances[person] += txn["amount"]
            else:
                balances[person] -= txn["amount"]
                
        # Format the balances into a strict fact sheet for the LLM
        ledger_facts = "\n".join([
            f"Fact: {p} owes you {amt} rs." if amt > 0 else 
            f"Fact: You owe {p} {abs(amt)} rs." if amt < 0 else 
            f"Fact: Your balance with {p} is settled (0 rs)."
            for p, amt in balances.items()
        ])

        # 3. Prompt the LLM with the injected math
        system_prompt = f"""
        You are the reasoning core of MemApp. Answer the user's question simply and conversationally.
        
        CRITICAL RULE:
        If the user asks about money, debts, or balances, you MUST use the "Hard Database Ledger Facts" below as the absolute truth. Do not attempt to recalculate the math yourself from the semantic memories. Just state the math from the Ledger Facts in a friendly way.
        
        --- Hard Database Ledger Facts (100% Accurate) ---
        {ledger_facts if ledger_facts else "No financial transactions recorded yet."}
        
        --- Semantic Context (For general questions) ---
        {context_text if context_text else "No relevant memories found."}
        """

        completion = await groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": q}
            ],
            model="llama-3.1-8b-instant",
            temperature=0, 
        )
        
        return {
            "query": q,
            "answer": completion.choices[0].message.content.strip(),
            "sources_utilized": len(response.data)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"RAG Engine Error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    # Read assigned port from cloud environment variable, fallback to 8000 locally
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False) # Turned off reload for production stability