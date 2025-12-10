import os
import json
import re
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from openai import AsyncAzureOpenAI, APIError
from proxy.utils import logger

router = APIRouter()

AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "https://YOUR_RESOURCE_NAME.openai.azure.com")
# Ensure no trailing slash
AZURE_ENDPOINT = AZURE_ENDPOINT.rstrip("/")

@router.post("/{path:path}")
async def azure_proxy(request: Request, path: str):
    """
    Handles Azure OpenAI requests using the official SDK.
    Expected path format: openai/deployments/{deployment}/chat/completions
    """
    # 1. Parse Deployment from Path
    # Pattern: openai/deployments/{deployment_id}/chat/completions
    match = re.search(r"deployments/([^/]+)/chat/completions", path)
    if not match:
        # Fallback or error if not a chat completion path we support
        # For now, we strictly support chat completions as requested by the move to SDK which is typed
        raise HTTPException(status_code=400, detail="Only chat/completions endpoints are supported with the SDK refactor.")
    
    deployment_id = match.group(1)
    
    # 2. Get API Key and Version
    api_key = request.headers.get("api-key")
    api_version = request.query_params.get("api-version", "2024-02-15-preview")
    
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing api-key header")

    # 3. Parse Body
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Log Input
    logger.info(f"Azure Request Input: {json.dumps(body)}")

    # 4. Prepare Client
    # Reuse global http client from app state
    client = AsyncAzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=api_key,
        api_version=api_version,
        http_client=request.app.state.http_client
    )

    # 5. Handle Parameters
    # Map body to SDK arguments
    # deployment_name is passed explicitly or via the client if configured, 
    # but `chat.completions.create` takes `model` which equates to deployment in Azure SDK usually,
    # OR we rely on the resource path. 
    # Actually, AsyncAzureOpenAI uses `azure_deployment` argument or `model`.
    # Let's use `model=deployment_id` which usually maps correctly in the python sdk for Azure.
    
    # Ensure stream_options for token counting in streams
    if body.get("stream", False):
        if "stream_options" not in body:
            body["stream_options"] = {"include_usage": True}
        elif isinstance(body["stream_options"], dict):
             body["stream_options"]["include_usage"] = True

    try:
        # Filter arguments to match create method signature roughly or pass **body
        # We need to ensure 'model' is set. 
        if "model" not in body:
             body["model"] = deployment_id

        response = await client.chat.completions.create(**body)
        
        if body.get("stream", False):
             return StreamingResponse(
                stream_response_generator(response),
                media_type="application/json"
            )
        else:
            # Non-streaming response is an object, need to serialize
            # response is a ChatCompletion object
            # We can use .model_dump() or .to_dict() (depending on version, model_dump is pydantic v2 in v1.x sdk)
            response_dict = response.model_dump()
            
            # Log usage
            usage = response_dict.get("usage", {})
            total_tokens = usage.get("total_tokens", 0)
            if total_tokens > 0:
                 logger.info(f"Azure Request Finished | Total Tokens: {total_tokens}")

            # Log Output (Text of choices)
            output_text = ""
            for choice in response_dict.get("choices", []):
                msg = choice.get("message", {})
                content = msg.get("content", "")
                if content:
                    output_text += content
            
            logger.info(f"Azure Response Output: {output_text}")

            return JSONResponse(content=response_dict)

    except APIError as e:
        logger.error(f"Azure OpenAI Error: {e}")
        # Return a json response with the error details
        return JSONResponse(status_code=e.status_code or 500, content={"error": e.message})
    except Exception as e:
        logger.error(f"Internal Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def stream_response_generator(response_iterator):
    """
    Yields chunks from the SDK async iterator and accumulates usage/text.
    """
    output_text = ""
    # The iterator yields ChatCompletionChunk objects
    async for chunk in response_iterator:
        # Convert chunk to dict for logging/serialization
        chunk_dict = chunk.model_dump()
        
        # Serialize to SSE format: data: {...}
        yield f"data: {json.dumps(chunk_dict)}\n\n"
        
        # Usage check
        usage = chunk_dict.get("usage", None)
        if usage:
            total = usage.get("total_tokens", 0)
            logger.info(f"Azure Stream Finished (Usage Reported) | Total Tokens: {total}")
        
        # Output text accumulation
        choices = chunk_dict.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})
            content = delta.get("content", "")
            if content:
                output_text += content

    yield "data: [DONE]\n\n"
    
    logger.info(f"Azure Stream Output: {output_text}")
