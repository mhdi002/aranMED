@echo off
set PYTHONIOENCODING=utf-8
set TRITON_URL=http://127.0.0.1:8002
set TRITON_TIMEOUT_SEC=300
set WHISPER_CHUNK_LENGTH_S=30
set WHISPER_STRIDE_LENGTH_S=0
set WHISPER_MAX_SHORTFORM_S=30
cd /d "C:\Users\mhf\Desktop\asr main\asr main\151-main\backend"
"C:\Users\mhf\Desktop\asr main\asr main\151-main\.venv\Scripts\python.exe" -m uvicorn app:app --host 0.0.0.0 --port 8010 > "C:\Users\mhf\Desktop\asr main\asr main\151-main\reports\backend_chunkfix.out.log" 2> "C:\Users\mhf\Desktop\asr main\asr main\151-main\reports\backend_chunkfix.err.log"
