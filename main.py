from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import threading
import uvicorn
from routes import router
from rfid import tcp_server, cleanup_tags

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

@app.on_event("startup")
def auto_connect():
    threading.Thread(target=tcp_server, daemon=True).start()
    threading.Thread(target=cleanup_tags, daemon=True).start()
    print("[AUTO] TCP server & cleanup started")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)