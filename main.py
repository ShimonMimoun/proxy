from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
import time
from proxy.utils import logger
from proxy.routers import azure, bedrock

app = FastAPI(title="AI Proxy", description="Async Proxy for Azure OpenAI and AWS Bedrock", version="0.1.0")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    Middleware to log request details and execution time.
    """
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    
    logger.info(
        f"Path: {request.url.path} | Method: {request.method} | "
        f"Status: {response.status_code} | Duration: {process_time:.4f}s"
    )
    return response

@app.get("/health")
async def health_check():
    return {"status": "ok", "message": "Proxy is running"}

app.include_router(azure.router, prefix="/azure")
app.include_router(bedrock.router, prefix="/bedrock")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
