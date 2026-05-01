import os
import sys
import json
import uuid
import asyncio
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel

from youtube_scraper import process_youtube_comments

app = FastAPI(title="YouTube Comment Studio API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ExtractRequest(BaseModel):
    url: str
    api_key: str
    skip: int = 0
    is_rescan: bool = False
    exclude_ids: list[int] = []

# In-memory store for tasks
# Format: { task_id: {"status": "running"|"completed"|"error", "events": Queue/List, "images": []} }
tasks = {}

@app.post("/api/extract")
async def start_extraction(req: ExtractRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    
    # Store task state
    tasks[task_id] = {
        "status": "running",
        "queue": asyncio.Queue(),
        "images": []
    }
    
    background_tasks.add_task(run_extraction_task, task_id, req.url, req.api_key, req.skip, req.is_rescan, req.exclude_ids)
    
    return {"task_id": task_id}

async def run_extraction_task(task_id: str, url: str, api_key: str, skip: int = 0, is_rescan: bool = False, exclude_ids: list = None):
    if exclude_ids is None:
        exclude_ids = []
    output_dir = os.path.join(os.getcwd(), "captured_comments", task_id)
    
    def thread_worker():
        async def async_worker():
            try:
                async for progress_event in process_youtube_comments(url, api_key, output_dir, task_id, skip, is_rescan, exclude_ids):
                    if progress_event["type"] == "complete":
                        tasks[task_id]["status"] = "completed"
                        tasks[task_id]["images"] = progress_event.get("images", [])
                    elif progress_event["type"] == "error":
                        tasks[task_id]["status"] = "error"
                        
                    asyncio.run_coroutine_threadsafe(tasks[task_id]["queue"].put(progress_event), main_loop)
                    
            except Exception as e:
                error_event = {"type": "error", "message": f"Server exception: {str(e)}"}
                tasks[task_id]["status"] = "error"
                asyncio.run_coroutine_threadsafe(tasks[task_id]["queue"].put(error_event), main_loop)
            finally:
                asyncio.run_coroutine_threadsafe(tasks[task_id]["queue"].put(None), main_loop)

        # Run with a brand new event loop in this thread
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        asyncio.run(async_worker())

    main_loop = asyncio.get_running_loop()
    import threading
    t = threading.Thread(target=thread_worker)
    t.start()

@app.get("/api/progress/{task_id}")
async def stream_progress(request: Request, task_id: str):
    if task_id not in tasks:
        return JSONResponse(status_code=404, content={"message": "Task not found"})
        
    async def event_generator():
        queue = tasks[task_id]["queue"]
        while True:
            if await request.is_disconnected():
                break
                
            event = await queue.get()
            if event is None:
                break
                
            yield json.dumps(event)
            
    return EventSourceResponse(event_generator())

@app.get("/api/images/{task_id}/{filename}")
async def get_image(task_id: str, filename: str):
    image_path = os.path.join(os.getcwd(), "captured_comments", task_id, filename)
    if os.path.exists(image_path):
        return FileResponse(image_path)
    return JSONResponse(status_code=404, content={"message": "Image not found"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

# Try to serve static files if they exist (for production deployment)
frontend_dist = os.path.join(os.path.dirname(os.getcwd()), "frontend", "dist")
if not os.path.exists(frontend_dist):
    frontend_dist = os.path.join(os.getcwd(), "frontend", "dist")

if os.path.exists(frontend_dist):
    # Mount the static files for everything that isn't under /api
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="static")
