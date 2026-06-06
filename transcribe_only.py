# FastAPI server to handle audio file uploads and transcribe them using Groq Whisper API.

# To test the endpoint, you can use the following curl command:
# PS D:\PROJECTS\MemApp\Backend\memory_backend> curl.exe -X POST "http://127.0.0.1:8000/api/memory" -F "file=@test.m4a"

import os
from fastapi import FastAPI, UploadFile, File, HTTPException
from groq import AsyncGroq
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allows any local file or web page to send data
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))

@app.post("/api/memory")
async def transcribe_audio(file: UploadFile = File(...)):
    # Validate file extension
    if not file.filename.endswith(('.wav', '.m4a', '.mp3', '.ogg', '.webm')):
        raise HTTPException(status_code=400, detail="Unsupported audio format.")
        
    try:
        # Read the uploaded file directly into memory bytes
        audio_bytes = await file.read()
        
        # Pass the bytes and the filename directly to Groq Whisper
        transcription = await client.audio.transcriptions.create(
            file=(file.filename, audio_bytes),
            model="whisper-large-v3",
            response_format="text",
            language="en"
        )
        
        return {
            "status": "success",
            # "filename": file.filename,
            "text": transcription
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Groq API Error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    # Run the server on port 8000
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


 