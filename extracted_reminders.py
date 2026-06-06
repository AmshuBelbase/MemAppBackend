import os
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from groq import AsyncGroq
from dotenv import load_dotenv
from supabase import create_client, Client
from sentence_transformers import SentenceTransformer
from datetime import datetime
import json

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

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")
supabase_client: Client = create_client(supabase_url, supabase_key)

# Initialize the local embedding model (768 dimensions to match your Supabase SQL)
print("Loading local embedding model... Please wait...")
embedding_model = SentenceTransformer("all-mpnet-base-v2")
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)