from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
import httpx
import os
import json
from proxy.utils import logger

router = APIRouter()

AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "https://YOUR_RESOURCE_NAME.openai.azure.com")
# Ensure no trailing slash
AZURE_ENDPOINT = AZURE_ENDPOINT.rstrip("/")

async def forward_request(request: Request, path: str):
    """
    Forwards the request to Azure OpenAI and handles response streaming/logging.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    # If streaming, ensure stream_options is set to get usage in the final chunk (OpenAI spec)
    if body.get("stream", False):
        if "stream_options" not in body:
            body["stream_options"] = {"include_usage": True}
        elif isinstance(body["stream_options"], dict):
             body["stream_options"]["include_usage"] = True

    # Log Input
    logger.info(f"Azure Request Input: {json.dumps(body)}")

    # Construct target URL
    # Path includes /openai/deployments/...
    url = f"{AZURE_ENDPOINT}/{path}?{request.url.query}"
    
    headers = dict(request.headers)
    # Remove host header to avoid SNI errors
    headers.pop("host", None)
    headers.pop("content-length", None) # Let httpx handle this

    client = httpx.AsyncClient()
    
    req = client.build_request(
        request.method,
        url,
        headers=headers,
        json=body,
        timeout=60.0
    )

    try:
        r = await client.send(req, stream=True)
    except Exception as e:
        logger.error(f"Failed to connect to Azure OpenAI: {e}")
        raise HTTPException(status_code=502, detail="Upstream connection failed")

    if r.status_code != 200:
        # Non-200 response, just stream it back without fancy logic
        return StreamingResponse(
            r.aiter_bytes(),
            status_code=r.status_code,
            media_type=r.headers.get("content-type"),
            background=None
        )

    # Handle Streaming vs Non-Streaming
    if body.get("stream", False):
        return StreamingResponse(
            stream_response_generator(r),
            status_code=r.status_code,
            media_type=r.headers.get("content-type")
        )
    else:
        # For non-streaming, we can read the whole body to count usage
        # But since we are inside a stream context (client.send(stream=True)), we need to read it.
        content = await r.aread()
        await client.aclose()
        
        try:
            response_json = json.loads(content)
            usage = response_json.get("usage", {})
            total_tokens = usage.get("total_tokens", 0)
            
            if total_tokens > 0:
                 logger.info(f"Azure Request Finished | Total Tokens: {total_tokens}")
            
            logger.info(f"Azure Response Output: {content.decode('utf-8', errors='replace')}")

            return JSONResponse(content=response_json, status_code=r.status_code)
        except Exception as e:
            logger.error(f"Error parsing azure response: {e}")
            return JSONResponse(content=json.loads(content), status_code=r.status_code)


async def stream_response_generator(response: httpx.Response):
    """
    Yields chunks from the upstream response and accumulates token usage.
    """
    output_text = ""
    # Azure/OpenAI stream format: data: {...} \n\n
    async for line in response.aiter_lines():
        if line:
            yield line + "\n"
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    chunk_data = json.loads(line[6:])
                    # Check for usage field in the last chunk (if stream_options included)
                    if "usage" in chunk_data and chunk_data["usage"]:
                        # Usage found in stream!
                        total = chunk_data["usage"].get("total_tokens")
                        logger.info(f"Azure Stream Finished (Usage Reported) | Total Tokens: {total}")
                    
                    choices = chunk_data.get("choices", [])
                    for choice in choices:
                        delta = choice.get("delta", {}) # Chat completion
                        text = choice.get("text", "")   # Legacy completion
                        
                        content = delta.get("content", "")
                        if content:
                            output_text += content
                        if text:
                            output_text += text

                except Exception:
                    pass
    
    await response.aclose()
    logger.info(f"Azure Stream Output: {output_text}")


@router.post("/{path:path}")
async def azure_proxy(request: Request, path: str):
    """
    Catch-all for Azure OpenAI paths.
    Expected path format: openai/deployments/{deployment}/chat/completions
    """
    return await forward_request(request, path)
