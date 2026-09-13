# MemApp - Local Development Guide

This guide explains exactly how to run both the FastAPI backend and the Flutter Android app locally on your machine for development.

## 📋 Prerequisites
Before you start, ensure you have the following installed:
- **Python 3.10+** (For the backend)
- **Flutter SDK** (For the mobile app)
- **Android SDK / adb** (Comes with Android Studio)
- **A physical Android phone** with USB Debugging enabled (or an Android Emulator).

---

## 🚀 Step 1: Start the Backend (FastAPI)
Your backend must be running first to receive API requests from the app.

1. Open a terminal (PowerShell or Command Prompt).
2. Navigate to the backend folder:
   ```bash
   cd C:\MemApp\Backend\memory_backend
   ```
3. Activate your virtual environment:
   ```bash
   .venv\Scripts\activate
   ```
4. Start the server (listening on all interfaces):
   ```bash
   python -m uvicorn main:app --host 0.0.0.0 --port 8000
   ```
   *(Keep this terminal window open!)*

---

## 🔌 Step 2: Bridge the USB Connection (ADB Reverse)
Because public Wi-Fi networks block devices from talking to each other, we tunnel the network connection directly through your physical USB cable.

1. Connect your Android phone to your laptop via USB cable.
2. Open a **new** terminal window.
3. Run the port-forwarding command:
   ```bash
   adb reverse tcp:8000 tcp:8000
   ```
   *(If successful, it will simply output `8000`. If it says "adb is not recognized", you may need to use the full path: `C:\Users\amsub\AppData\Local\Android\Sdk\platform-tools\adb.exe reverse tcp:8000 tcp:8000`)*

---

## 📱 Step 3: Run the Flutter App
Now that the server is running and the USB bridge is active, you can launch the app.

1. In your terminal, navigate to the Flutter project folder:
   ```bash
   cd C:\MemApp\voice_memory_app
   ```
2. Verify your phone is connected and recognized:
   ```bash
   flutter devices
   ```
3. Build and launch the app on your phone:
   ```bash
   flutter run
   ```

### Hot Reloading
If you make changes to the Flutter UI code in VS Code, simply click into the terminal where `flutter run` is active and press the **`r`** key to instantly refresh the app without rebuilding!

---

## 🌐 Testing the Cloud Fallback
If you want to test your Render cloud deployment (`https://memappbackend.onrender.com`), simply:
1. Go to the terminal running `uvicorn` (Step 1) and press `Ctrl + C` to kill the local server.
2. Go to your Flutter app and press a button (like Search or Record).
3. The app will try `127.0.0.1:8000`, fail instantly, and gracefully fall back to your live Cloud server!
