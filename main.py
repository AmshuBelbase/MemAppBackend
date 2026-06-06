# PS D:\PROJECTS\MemApp\Backend> curl.exe -X POST "http://127.0.0.1:8000/api/internal/check-reminders" -H "Authorization: Bearer api_key"


import os
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from groq import AsyncGroq
from dotenv import load_dotenv
from supabase import create_client, Client
from sentence_transformers import SentenceTransformer 
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

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")
supabase_client: Client = create_client(supabase_url, supabase_key)

# Initialize the local embedding model (768 dimensions to match your Supabase SQL)
print("Loading local embedding model... Please wait...")
# embedding_model = SentenceTransformer("all-mpnet-base-v2")
embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
print("Embedding model loaded successfully.")


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
        text_embedding = embedding_model.encode(raw_text).tolist()

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


@app.get("/api/search")
async def search_memories(q: str):
    if not q.strip():
        raise HTTPException(status_code=400, detail="Search query cannot be empty.")
        
    try:
        # 1. Convert the plain-text search query into a vector match string
        query_vector = embedding_model.encode(q.strip()).tolist()
        
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
            
        # sent_count = 0
        # for task in due_tasks:
        #     # TODO: In Step 4.3, we will swap this print statement with actual 
        #     # Firebase Push Notification and Resend Email API calls.
        #     print("\n" + "="*40)
        #     print(f"🚨 NOTIFICATION TRIGGERED: {task['task_name']}")
        #     print(f"⏰ DUE: {task['due_datetime']}")
        #     print("="*40 + "\n")
            
        #     # 4. Mark the task as 'sent' so we don't notify you twice
        #     supabase_client.table("reminders") \
        #         .update({"status": "sent"}) \
        #         .eq("id", task["id"]) \
        #         .execute()
                
        #     sent_count += 1

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


# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

if __name__ == "__main__":
    import uvicorn
    # Read assigned port from cloud environment variable, fallback to 8000 locally
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False) # Turned off reload for production stability