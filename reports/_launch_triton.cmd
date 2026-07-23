@echo off
set WHISPER_DEVICE=cuda
set WHISPER_WARMUP=1
set WHISPER_CHUNK_LENGTH_S=30
set WHISPER_STRIDE_LENGTH_S=0
set WHISPER_MAX_SHORTFORM_S=30
set WHISPER_OUTPUT_ENGLISH=1
set WHISPER_TASK=translate
set PYTHONIOENCODING=utf-8
cd /d "C:\Users\mhf\Desktop\asr main\asr main\151-main"
"C:\Users\mhf\Desktop\asr main\asr main\151-main\.venv\Scripts\python.exe" "C:\Users\mhf\Desktop\asr main\asr main\151-main\deploy\triton\compat_http_server.py" > "C:\Users\mhf\Desktop\asr main\asr main\151-main\reports\triton_compat_chunkfix.out.log" 2> "C:\Users\mhf\Desktop\asr main\asr main\151-main\reports\triton_compat_chunkfix.err.log"
