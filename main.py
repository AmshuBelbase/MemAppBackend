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
            model="llama3-8b-8192",
            temperature=0, 
        )
        
        response_text = completion.choices[0].message.content.strip()
        print(f"LLM Raw Output: {response_text}") # Turned on debug print
        
        # Sometimes the LLM wraps the response in ```json ... ```
        if response_text.startswith("```json"):
            response_text = response_text.replace("```json", "", 1)
        if response_text.endswith("```"):
            response_text = response_text.rsplit("```", 1)[0]
        response_text = response_text.strip()
            
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
        print(f"Extraction failed: {e}")
        return 0


# --- API ENDPOINTS ---

@app.get("/api/memories")
async def get_all_memories():
    try:
        # Fetch the top 100 recent memories (we exclude the embedding array to save bandwidth)
        response = supabase_client.table("memories") \
            .select("id, raw_text") \
            .order("id", desc=True) \
            .limit(100) \
            .execute()
            
        return {"status": "success", "results": response.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

# Create a data model for the incoming text
class TextMemoryRequest(BaseModel):
    text: str

@app.post("/api/memory/text")
async def store_text_memory(request: TextMemoryRequest):
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
            "embedding": text_embedding
        }
        
        response = supabase_client.table("memories").insert(data).execute()
        memory_id = response.data[0]["id"]

        # 3. Run the LLM extraction in the background (if you have this function active)
        tasks_found = await extract_reminders(raw_text, memory_id)
        
        return {
            "status": "success",
            "saved_text": raw_text,
            "database_id": memory_id,
            "tasks_scheduled": tasks_found
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server Pipeline Error: {str(e)}")

# This endpoint handles the entire pipeline: audio transcription, embedding generation, and database storage.
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
        
        return {
            "status": "success",
            "transcription": raw_text,
            "database_id": memory_id,
            "tasks_scheduled": tasks_found
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
async def update_memory(memory_id: str, request: UpdateMemoryRequest):
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

        return {"status": "success", "message": "Memory updated successfully."}
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
    system_prompt = """
    You are a precise financial extraction AI. Analyze the user's text and extract the transaction details.
    Assume the user speaking is named "Self".
    Calculate the total amounts if quantities and unit prices are given.
    
    Return ONLY a raw JSON ARRAY of objects. Do not include markdown formatting or backticks.
    Even if there is only one transaction, you MUST return it inside an array [].
    
    Each object must have these keys:
    - "creditor": The person who is owed the money (usually "Self"). Format as Title Case.
    - "debtor": The person who owes the money. Format as Title Case.
    - "amount": The total numerical amount calculated. (Float)
    - "description": A short summary of what it was for.
    
    Example input: "I bought 6 bags of 1200 each and gave 2 bags to siddharth and 3 to aarush."
    Example output: [
      {"creditor": "Self", "debtor": "Siddharth", "amount": 2400.0, "description": "2 bags at 1200 each"},
      {"creditor": "Self", "debtor": "Aarush", "amount": 3600.0, "description": "3 bags at 1200 each"}
    ]
    """

    try:
        completion = await groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": req.text}
            ],
            model="llama-3.3-70b-versatile",
            temperature=0, 
        )
        
        response_text = completion.choices[0].message.content.strip()

        # Parse the response into a list of dictionaries
        raw_transactions = json.loads(response_text)
        
        # Strip out any transactions where the creditor and debtor are the same person
        valid_transactions = [
            t for t in raw_transactions 
            if t["creditor"].lower() != t["debtor"].lower()
        ]
        
        if valid_transactions:
            db_response = supabase_client.table("transactions").insert(valid_transactions).execute()
            
            # Create a nice summary string for the API response
            messages = [f"{t['debtor']} owes {t['creditor']} {t['amount']} rs" for t in valid_transactions]
            summary = " | ".join(messages)
            
            return {
                "status": "success",
                "message": f"Recorded: {summary}",
                "data": db_response.data
            }
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
            model="llama-3.1-8b-instant",
            temperature=0,
        )
        
        target_person = completion.choices[0].message.content.strip()
        
        # 2. Let the deterministic Database handle the math
        # Get what they owe you
        owed_to_self = supabase_client.table("transactions") \
            .select("amount") \
            .eq("creditor", "Self") \
            .eq("debtor", target_person) \
            .execute()
            
        # Get what you owe them
        owed_to_target = supabase_client.table("transactions") \
            .select("amount") \
            .eq("creditor", target_person) \
            .eq("debtor", "Self") \
            .execute()
            
        sum_owed_to_self = sum(item['amount'] for item in owed_to_self.data) if owed_to_self.data else 0
        sum_owed_to_target = sum(item['amount'] for item in owed_to_target.data) if owed_to_target.data else 0
        
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
            model="llama-3.3-70b-versatile",
            temperature=0, # Highly deterministic and factual output
        )
        
        return {
            "query": q,
            "answer": completion.choices[0].message.content.strip(),
            "sources_utilized": len(retrieved_notes)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"RAG Engine Error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    # Read assigned port from cloud environment variable, fallback to 8000 locally
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False) # Turned off reload for production stability