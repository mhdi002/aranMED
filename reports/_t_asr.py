import wave, tempfile, httpx, numpy as np, time, os
from pathlib import Path
sr=16000
audio=(np.random.randn(int(sr*1.5))*0.01).astype(np.float32)
pcm=(np.clip(audio,-1,1)*32767).astype(np.int16)
p=Path(tempfile.gettempdir())/"aranmed_verify.wav"
with wave.open(str(p),"wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())
print("wav", p, p.stat().st_size)
t0=time.time()
with httpx.Client(base_url="http://127.0.0.1:8010", timeout=600.0) as c:
    with open(p,"rb") as f:
        r=c.post("/api/transcribe", files={"file":("verify.wav", f, "audio/wav")})
print("status", r.status_code, "elapsed", round(time.time()-t0,1))
print(r.text[:500])
