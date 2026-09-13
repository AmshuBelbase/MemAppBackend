# PS D:\PROJECTS\MemApp\Backend> curl.exe -X POST "http://127.0.0.1:8000/api/internal/check-reminders" -H "Authorization: Bearer api_key"


import os
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
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
from pydantic import BaseModel

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
    If a task is implied but no specific time is given, schedule it for exactly 15 minutes from the current time as a default.
    Return a strictly valid JSON object with a single key "reminders" containing an array of objects.
    Each object must have exactly two keys: 
    - "task_name": A short, clear string. If monetary values are involved, assume 'rs' or 'INR' as default if currency is not mentioned.
    - "due_datetime": A strict UTC ISO 8601 formatted timestamp ending in 'Z' (YYYY-MM-DDTHH:MM:SSZ). Do NOT use local timezone offsets.
    If no events are mentioned, return {{"reminders": []}}.
    """

    try:
        completion = await groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            model="openai/gpt-oss-120b",
            temperature=0, 
            response_format={"type": "json_object"}
        )
        
        response_text = completion.choices[0].message.content.strip()
        print(f"LLM Raw Output: {response_text}")
            
        # Parse the strict JSON output
        data = json.loads(response_text)
        reminders = data.get("reminders", [])
        
        for reminder in reminders:
            supabase_client.table("reminders").insert({
                "memory_id": memory_id,
                "task_name": reminder["task_name"],
                "due_datetime": reminder["due_datetime"]
            }).execute()
            
        return len(reminders)
        
    except Exception as e:
        print(f"Extraction failed: {e}")
        return 0


async def extract_transactions(text: str, memory_id: str = None):
    system_prompt = """
    You are a precise financial extraction AI. Analyze the user's text and extract the transaction details.
    Assume the user speaking is named "Self".
    Calculate the total amounts if quantities and unit prices are given.
    
    Return a strictly valid JSON object with a single key "transactions" containing an array of objects.
    
    Each object must have these keys:
    - "creditor": The person who is owed the money (usually "Self"). Format as Title Case.
    - "debtor": The person who owes the money. Format as Title Case.
    - "amount": The numerical amount (float).
    - "currency": Always use 'INR' unless explicitly stated otherwise.
    - "description": A short summary of the transaction (e.g. "Lunch", "Movie tickets", "Pending payment from Aastha"). Preserve the original tense! If it's a future debt (e.g., "receive 500 from X"), do not say "Received". Use "To receive" or preserve the exact intent.
    
    If no transactions are found, return {"transactions": []}.
    """

    try:
        completion = await groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            model="openai/gpt-oss-120b",
            temperature=0, 
            response_format={"type": "json_object"}
        )
        
        response_text = completion.choices[0].message.content.strip()
        print(f"Finance LLM Raw Output: {response_text}")

        # Parse the strict JSON output
        data = json.loads(response_text)
        raw_transactions = data.get("transactions", [])
        
        valid_transactions = []
        for t in raw_transactions:
            if t.get("creditor", "").lower() != t.get("debtor", "").lower():
                # Inject the memory_id so we can trace the transaction back to its voice note
                if memory_id:
                    t["memory_id"] = memory_id
                valid_transactions.append(t)
        
        if valid_transactions:
            supabase_client.table("transactions").insert(valid_transactions).execute()
            
        return len(valid_transactions)
            
    except Exception as e:
        print(f"Transaction extraction failed: {e}")
        return 0


# --- API ENDPOINTS ---

@app.get("/api/memories")
async def get_all_memories():
    try:
        # Fetch the top 100 recent memories (we exclude the embedding array to save bandwidth)
        response = supabase_client.table("memories") \
            .select("id, raw_text, created_at, source, is_starred") \
            .order("id", desc=True) \
            .limit(100) \
            .execute()
            
        return {"status": "success", "results": response.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

# Create a data model for the incoming text
class TextMemoryRequest(BaseModel):
    text: str
    source: str = "text"

@app.post("/api/memory/text")
async def store_text_memory(request: TextMemoryRequest, background_tasks: BackgroundTasks):
    raw_text = request.text.strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
        
    try:
        # 1. Generate vector embedding via the Gemini SDK
        embedding_result = genai_client.models.embed_content(
            model="gemini-embedding-001",
            contents=raw_text,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=768 
            )
        )
        
        text_embedding = embedding_result.embeddings[0].values

        # 2. Store into Supabase Table
        data = {
            "raw_text": raw_text,
            "embedding": text_embedding,
            "source": request.source
        }
        
        response = supabase_client.table("memories").insert(data).execute()
        memory_id = response.data[0]["id"]

        # 3. Run the LLM extractors in the background
        background_tasks.add_task(extract_reminders, raw_text, memory_id)
        background_tasks.add_task(extract_transactions, raw_text, memory_id)
        
        return {
            "status": "success",
            "saved_text": raw_text,
            "database_id": memory_id,
            "message": "Memory saved. Reminders and Transactions are processing in the background."
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server Pipeline Error: {str(e)}")

# This endpoint handles just the transcription (allows user to review before saving)
@app.post("/api/transcribe")
async def transcribe_audio_only(file: UploadFile = File(...)):
    # Validate allowed formats
    if not file.filename.endswith(('.wav', '.m4a', '.mp3', '.ogg', '.webm')):
        raise HTTPException(status_code=400, detail="Unsupported audio format.")
        
    try:
        # Transcribe via Groq Whisper
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

        return {
            "status": "success",
            "transcription": raw_text
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription Error: {str(e)}")

# This endpoint handles the entire pipeline: audio transcription, embedding generation, and database storage.
@app.post("/api/memory")
async def transcribe_and_store_audio(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
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
            "embedding": text_embedding,
            "source": "audio"
        }
        
        response = supabase_client.table("memories").insert(data).execute()

        memory_id = response.data[0]["id"]

        # Run the LLM extractors in the background
        background_tasks.add_task(extract_reminders, raw_text, memory_id)
        background_tasks.add_task(extract_transactions, raw_text, memory_id)
        
        return {
            "status": "success",
            "transcription": raw_text,
            "database_id": memory_id,
            "message": "Audio saved. Reminders and Transactions are processing in the background."
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server Pipeline Error: {str(e)}")

# This endpoint takes a natural language query, converts it to a vector, and retrieves relevant memories from Supabase.
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

# This endpoint is designed to be triggered by a secure cron job every hour. It checks for any reminders that are due within the next hour and sends email notifications accordingly.
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

# --- EDIT AND DELETE ENDPOINTS ---

@app.delete("/api/memory/{memory_id}")
async def delete_memory(memory_id: str):
    try:
        # Delete related extractions to keep ledger clean
        supabase_client.table("reminders").delete().eq("memory_id", memory_id).execute()
        supabase_client.table("transactions").delete().eq("memory_id", memory_id).execute()
        
        # Delete the row where the ID matches
        response = supabase_client.table("memories").delete().eq("id", memory_id).execute()
        
        # Supabase returns the deleted rows in response.data. If empty, it didn't exist.
        if not response.data:
            raise HTTPException(status_code=404, detail="Memory not found.")
            
        return {"status": "success", "message": f"Memory {memory_id} deleted."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

# Create a data model for the update request
class UpdateMemoryRequest(BaseModel):
    text: str

@app.put("/api/memory/{memory_id}")
async def update_memory(memory_id: str, request: UpdateMemoryRequest, background_tasks: BackgroundTasks):
    raw_text = request.text.strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
        
    try:
        # 1. Generate a NEW vector embedding for the updated text
        embedding_result = genai_client.models.embed_content(
            model="gemini-embedding-001",
            contents=raw_text,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=768 
            )
        )
        new_embedding = embedding_result.embeddings[0].values

        # 2. Update the row in Supabase
        data = {
            "raw_text": raw_text,
            "embedding": new_embedding
        }
        response = supabase_client.table("memories").update(data).eq("id", memory_id).execute()
        
        if not response.data:
            raise HTTPException(status_code=404, detail="Memory not found.")

        # 3. Synchronize Extractions
        # Delete old extractions linked to this memory
        supabase_client.table("reminders").delete().eq("memory_id", memory_id).execute()
        supabase_client.table("transactions").delete().eq("memory_id", memory_id).execute()
        
        # Re-run extractors in background on the fresh text
        background_tasks.add_task(extract_reminders, raw_text, memory_id)
        background_tasks.add_task(extract_transactions, raw_text, memory_id)

        return {"status": "success", "message": "Memory updated and extractions resynced."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

# --- FINANCE MANAGER EXTENSION ---

class TextRequest(BaseModel):
    text: str

@app.post("/api/finance/record")
async def record_transaction(req: TextRequest):
    """
    Parses unstructured text, calculates totals via LLM reasoning, 
    and saves multiple transactions to the Supabase ledger.
    """
    try:
        count = await extract_transactions(req.text)
        if count > 0:
            return {"status": "success", "message": f"Successfully recorded {count} transactions."}
        else:
            return {"status": "skipped", "message": "No valid external debts detected."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to record transaction: {str(e)}")

@app.get("/api/finance/balance")
async def check_balance(q: str):
    """
    Extracts the target person's name using Groq, then relies entirely 
    on the Supabase database to calculate the pinpoint accurate net balance.
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty.")
        
    # 1. Use LLM just to figure out WHO we are asking about
    extraction_prompt = """
    Extract the name of the person the user is asking about from the query. 
    Return ONLY the person's name in Title Case. No punctuation, no extra words.
    Example input: "what is transaction status between me and siddharth"
    Example output: Siddharth
    """
    
    try:
        completion = await groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": extraction_prompt},
                {"role": "user", "content": q}
            ],
            model="openai/gpt-oss-120b",
            temperature=0,
        )
        
        target_person = completion.choices[0].message.content.strip()
        
        # 2. Let the deterministic Database handle the math with Fuzzy Matching
        all_transactions = supabase_client.table("transactions").select("*").execute()
        
        target_lower = target_person.lower()
        
        sum_owed_to_self = 0
        sum_owed_to_target = 0
        
        if all_transactions.data:
            for t in all_transactions.data:
                creditor = t.get("creditor", "").lower()
                debtor = t.get("debtor", "").lower()
                amount = float(t.get("amount", 0))
                
                # If the target person is the debtor, they owe Self
                if target_lower in debtor and "self" in creditor:
                    sum_owed_to_self += amount
                    
                # If the target person is the creditor, Self owes them
                elif target_lower in creditor and "self" in debtor:
                    sum_owed_to_target += amount
                    
        net_balance = sum_owed_to_self - sum_owed_to_target
        
        # 3. Formulate the response programmatically for 100% accuracy
        if net_balance > 0:
            status_message = f"{target_person} has to pay me {net_balance} rs."
        elif net_balance < 0:
            status_message = f"I have to pay {target_person} {abs(net_balance)} rs."
        else:
            status_message = f"All settled up with {target_person}."

        return {
            "status": "success",
            "target": target_person,
            "net_balance": net_balance,
            "response": status_message
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to calculate balance: {str(e)}")



@app.get("/api/chat")
async def chat_with_memories(q: str):
    if not q:
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required")

    try:
        # 1. Convert the natural language question into a vector using the new GenAI SDK
        query_result = genai_client.models.embed_content(
            model="gemini-embedding-001",
            contents=q.strip(),
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=768
            )
        )
        query_vector = query_result.embeddings[0].values

        # 2. Search Supabase for the top 10 most relevant memories matching the query
        # We increase the count to 10 so the LLM gets historical depth for math tracking
        response = supabase_client.rpc(
            "match_memories",
            {
                "query_embedding": query_vector,
                "match_threshold": 0.2, # Lower threshold slightly to catch broad context
                "match_count": 10
            }
        ).execute()

        retrieved_notes = response.data
        
        # 3. Format the retrieved memories into a structured context string for the LLM
        context_text = ""
        for index, note in enumerate(retrieved_notes):
            context_text += f"Memory [{index + 1}]: {note['raw_text']}\n"

        system_prompt = f"""
        You are the reasoning core of MemApp, a personal cognitive assistant. 
        Your objective is to answer the user's question about their transactions using only the verified facts inside the provided context.
        
        Strict Calculation Rules (Process Internally):
        - Sum up all transactions involving the person mentioned.
        - Anything I paid/lent counts as positive (+). Anything they paid/returned counts as negative (-).
        - Compute the final net total carefully.
        
        Formatting Rules (Mandatory):
        - DO NOT output step-by-step rules, numbers, headers, or bullet points. 
        - Keep the vocabulary extremely simple, direct, and conversational.
        - State clearly what each party did, followed by the exact net result.
        
        Example Output Style:
        "Siddharth gave you 50. You gave him 200. Siddharth paid you 100. So now he has to give you 500 only."
        
        Retrieved Memories Context:
        \"\"\"
        {context_text if context_text else "No relevant records found in the user's database."}
        \"\"\"
        """

        # # 4. Construct the system prompt to turn Llama 3.1 into a reasoning ledger
        # system_prompt = f"""
        # You are the reasoning core of MemApp, a personal cognitive assistant specializing in financial ledger tracking. 
        # Your objective is to answer the user's question using only the verified facts inside the provided context.
        
        # Strict Ledger Bookkeeping Protocol:
        # 1. Identify all transactions involving the specified individual.
        # 2. Assign a mathematical valuation using a strict sign convention from MY perspective:
        #    - Outgoing Transactions (+): Any money I paid out, lent, or spent on their behalf means they owe me. Mark as POSITIVE (+).
        #    - Incoming Transactions (-): Any money they gave me, paid me, or I borrowed means I owe them. Mark as NEGATIVE (-).
        # 3. Do the step-by-step math evaluation (Sum up the values) but no need to give in final output.
        # 4. Apply the Final Ledger Rules:
        #    - If the net sum is POSITIVE (+), that person owes ME that final amount.
        #    - If the net sum is NEGATIVE (-), I owe THAT PERSON that final amount.
        # 5. If the context does not contain any relevant transactions, respond with "Based on the provided information, there are no financial interactions to report regarding this individual."
        # 6. Do NOT make any assumptions or add information that is not explicitly stated in the context. If the context lacks sufficient information to answer the question, say "The provided context does not contain enough information to determine the financial relationship."
        # 7. No need to show the calculation and evaluation if the context explicitly states the final amount owed. In that case, just confirm the final amount and who owes whom.
        
        # Final Output Format (Strictly follow this format):
        # "Siddharth gave you 30. You gave him 149. He again gave you 20. So now he has to give you 99 only."

        # Retrieved Memories Context:
        # \"\"\"
        # {context_text if context_text else "No relevant records found in the user's database."}
        # \"\"\"
        # """

        # 5. Query Llama 3.1 to compute and rationalize the final answer
        completion = await groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Question: {q}"}
            ],
            model="openai/gpt-oss-120b",
            temperature=0, # Highly deterministic and factual output
        )
        
        return {
            "query": q,
            "answer": completion.choices[0].message.content.strip(),
            "sources_utilized": len(retrieved_notes)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"RAG Engine Error: {str(e)}")

# --- DASHBOARD ENDPOINTS ---

@app.get("/api/memories")
async def get_all_memories():
    try:
        response = supabase_client.table("memories").select("*").order("created_at", desc=True).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

@app.delete("/api/memory/{memory_id}")
async def delete_memory(memory_id: str):
    try:
        response = supabase_client.table("memories").delete().eq("id", memory_id).execute()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

# Data model for batch deletion
class DeleteMemoriesRequest(BaseModel):
    ids: list[str]

@app.delete("/api/memories")
async def delete_multiple_memories(request: DeleteMemoriesRequest):
    try:
        if not request.ids:
            return {"status": "success"}
        # Supabase in filter accepts a list of values
        response = supabase_client.table("memories").delete().in_("id", request.ids).execute()
        return {"status": "success", "deleted_count": len(response.data) if response.data else 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

class StarMemoryRequest(BaseModel):
    is_starred: bool

@app.put("/api/memory/{memory_id}/star")
async def toggle_star_memory(memory_id: str, request: StarMemoryRequest):
    try:
        response = supabase_client.table("memories").update({"is_starred": request.is_starred}).eq("id", memory_id).execute()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

@app.get("/api/reminders")
async def get_all_reminders():
    try:
        response = supabase_client.table("reminders").select("*").order("due_datetime", desc=False).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

class UpdateReminderRequest(BaseModel):
    status: str

@app.put("/api/reminders/{reminder_id}")
async def update_reminder_status(reminder_id: str, request: UpdateReminderRequest):
    if request.status not in ["pending", "completed"]:
        raise HTTPException(status_code=400, detail="Invalid status. Must be 'pending' or 'completed'.")
        
    try:
        response = supabase_client.table("reminders").update({"status": request.status}).eq("id", reminder_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="Reminder not found.")
        return {"status": "success", "message": "Reminder updated."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

@app.get("/api/transactions")
async def get_all_transactions():
    try:
        response = supabase_client.table("transactions").select("*").order("created_at", desc=True).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    # Read assigned port from cloud environment variable, fallback to 8000 locally
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False) # Turned off reload for production stability