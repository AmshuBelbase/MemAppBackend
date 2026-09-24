# Welcome to Katch

## What is Katch?
Katch is your ultimate personal assistant and "second brain" that fits right in your pocket. Designed for people on the move, it allows you to capture fleeting thoughts, track your expenses, and set reminders instantly using just your voice. 

Instead of fumbling through complex menus, typing out long notes, or using three different apps for your calendar, finances, and to-do lists, you simply speak naturally. Katch listens, understands what you need, and organizes your life for you AUTOMATICALLY !!

## Magic Features
- **Unmatched Ease of Use:** Forget complex menus or endless typing. Just tap the microphone and speak your mind—or type if you prefer. Katch's AI instantly parses your input, understands the context, and automatically organizes everything into tasks, budgets, and memories without any manual data entry.
- **Ask Your Memory Vault:** Never forget a detail again. You can chat with your memories like you would a real person. Ask, *"What book did John recommend to me last month?"* and Katch will instantly find the answer from your past notes.
- **Advanced Smart Reminders:** 
  - Say, *"Remind me to call the dentist tomorrow at 3 PM"* and it auto-schedules. 
  - Need repetition? Tell it to *"remind me to take pills every day at 8 AM"* and it sets a **recurring reminder**.
  - **Offline Alarms:** Katch uses native device alarms. Even if you are entirely offline, on an airplane, or the app is closed, your phone will ring to remind you.
- **Stay on Track:** Beyond offline alarms, Katch proactively taps you on the shoulder with online push notifications and a helpful email digest so nothing slips through the cracks.
- **Comprehensive Finance Tracking:**
  - **Personal Budgeting:** Say, *"I spent Rs 45 on groceries."* Katch extracts the expense, maps it to a customizable category (like 'Groceries'), and updates your dynamic Pie/Bar charts and net balance KPIs.
  - **Splitwise Integration:** Going out with friends? Say, *"Split a 200 Rs taxi with Sarah & John."* Katch detects the split, automatically calculates who owes whom, and perfectly tracks your debts & credits in a dedicated Split section.
- **Bank-Level Security:** Secure your data with robust Google OAuth or standard email OTP. Only you can access your personal vault, backed by Row-Level Security in the cloud.

## Who is this App For?
- **Busy Professionals & Creatives:** Perfect for when you get your best ideas while driving, walking the dog, or simply away from a keyboard.
- **Freelancers & Roommates:** Effortlessly track daily expenses, manage receipts, or split bills on the fly without navigating clunky accounting apps.
- **Productivity Enthusiasts:** Anyone looking for a frictionless personal assistant that actually remembers everything you tell it—and proactively reminds you when it matters most.

## How to Get the APP?
1. **Install App:** Download and Install from [link](https://github.com/AmshuBelbase/Katch/releases/latest) by clicking katch-v(latest_version).apk under Assets.
2. **Sign Up:** Create a free account securely using your email or Google account.
3. **Speak Your Mind:** Tap the microphone on the home screen and talk naturally. Try saying: *"I need to renew my passport before next Friday"* or *"Paid 12 rupees for a tea"*.
4. **Let the App Work:** Sit back and relax. Katch will instantly organize your note, extract the "Renew Passport" task, and quietly schedule your reminder.
5. **Chat & Search:** Need to find an old thought? Go to the Chat tab and just ask for it.
6. **Get Reminded:** When a deadline approaches, your phone will buzz to remind you. It’s that easy!


# Are you a developer like me?

### KATCH - Backend

This is the FastAPI backend for the Katch project. It acts as the core engine powering the voice transcriptions, AI processing, scheduling, and notifications.

## Features

- **Frictionless Ingestion**: Accepts raw audio or text. Users simply record voice and the AI handles the rest automatically. Streams audio to Groq's `whisper-large-v3` for instant, high-accuracy text transcription.
- **AI Brain & Vector Search**: Vectorizes raw text using Google's Gemini embeddings (`gemini-embedding-001`) and performs fast Cosine Similarity searches via Supabase's `pgvector`.
- **Advanced LLM Extraction & Scheduling**: Background workers process transcripts to autonomously detect tasks, dates, and recurring schedules. Supports extracting complex recurrence rules (e.g. daily, weekly).
- **Financial Categorization (Personal & Split)**: Intelligent classification of expenses, incomes, and split transactions. Automatically assigns personal transactions to custom categories, while separately calculating debt balances for Splitwise-style transactions.
- **Automated Notifications & Offline Support**: A cron-triggered worker dispatches push notifications (via FCM) and consolidated email digests (via Brevo API). The backend perfectly formats timestamps so the frontend can trigger offline device alarms that ring unconditionally.
- **Custom OTP Authentication**: Implements dynamic 6-digit OTP generation and verification endpoints, seamlessly integrating with Supabase Auth while utilizing custom Brevo email templates.

## Setup Instructions

1. Ensure you have Python 3.10+ installed.
2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows use: .venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Configure your environment variables in `.env`:
   - `GROQ_API_KEY`
   - `GEMINI_API_KEY`
   - `SUPABASE_URL`
   - `SUPABASE_KEY`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `BREVO_API_KEY`
   - `BREVO_SENDER_EMAIL`
5. Place your Firebase Admin SDK configuration at `firebase-adminsdk.json`.
6. Run the server:
   ```bash
   uvicorn main:app --reload --host 0.0.0.1 --port 8000
   ```
