from fastapi import APIRouter, Request, HTTPException, Response
from fastapi.responses import StreamingResponse, JSONResponse
import aioboto3
import os
import json
from proxy.utils import logger

router = APIRouter()
session = aioboto3.Session()
REGION = os.getenv("AWS_REGION", "eu-central-1")
ROLE_ARN = os.getenv("AWS_ROLE_ARN")

        )
        return resp["Credentials"]

# Client Cache to avoid session creation overhead
# Key: (region, role_arn or None) -> client
# Since aioboto3 clients are async context managers, we need to manage their lifecycle carefully.
# Simplified approach: Use a global session and just create clients. aioboto3 session is thread safe.
# Actually, best practice for high scale with aioboto3 is to create one session per event loop (which we effectively have here)
# and reuse clients? No, clients in aioboto3 are context managers.
# Strategy: Use 'service' abstraction or keep session global (already is).
# The overhead is mainly in `session.client` creation and credential resolution.
# We will optimize by resolving credentials once per request (or caching credentials?) and ensuring session is reused.
# Actually, the biggest gain is reusing the underlying botocore session logic.
# The `session` variable is already global in this file. That helps.
# But we can't easily cache open 'client' objects because they are async context managers meant for 'async with'.
# However, we can use `await session.create_client(...)` without `async with` if we manage closing,
# BUT simpler optimization for now:
# 1. Reuse credentials if possible (boto3 does this internally via its credential provider chain).
# 2. Reusing session is key. We are doing that.
# 3. For assumes role, we are fetching credentials every time! This is SLOW. We must cache the assumed role credentials.

import time
_creds_cache = {"data": None, "expiry": 0}

async def get_credentials():
    """
    Returns credentials dictionary. Caches assumed role credentials to avoid calling STS every request.
    """
    if not ROLE_ARN:
        return None
    
    now = time.time()
    if _creds_cache["data"] and _creds_cache["expiry"] > now:
        return _creds_cache["data"]
        
    async with session.client("sts", region_name=REGION) as sts:
        resp = await sts.assume_role(
            RoleArn=ROLE_ARN,
            RoleSessionName="ProxySession",
            DurationSeconds=3600
        )
        creds = resp["Credentials"]
        _creds_cache["data"] = creds
        # Expire 5 minutes before actual expiration
        _creds_cache["expiry"] = creds["Expiration"].timestamp() - 300 
        return creds

@router.post("/runtime/{operation}")
async def bedrock_runtime(operation: str, request: Request):
    """
    Handles Bedrock Runtime operations: InvokeModel, Converse, etc.
    Expected custom header for model-id if not in body, though typically in path for generic invoke.
    However, aioboto3 client methods take arguments matching the API.
    We receive a JSON body matching the boto3 arguments for the operation.
    """
    body = await request.json()
    
    method_name = operation_to_method(operation)
    
    # Check if this is a streaming operation
    is_stream = method_name in ["invoke_model_with_response_stream", "converse_stream"]
    
    creds = await get_credentials()
    kwargs = {"region_name": REGION}
    if creds:
        kwargs.update({
            "aws_access_key_id": creds["AccessKeyId"],
            "aws_secret_access_key": creds["SecretAccessKey"],
            "aws_session_token": creds["SessionToken"]
        })

    
    # Log Input
    logger.info(f"Bedrock Runtime Input ({operation}): {json.dumps(body)}")

            if is_stream:
                # Pass parameters to generator to create client inside
                return StreamingResponse(
                    bedrock_stream_generator(body, method_name, kwargs, operation),
                    media_type="application/json"
                )
            else:
                async with session.client("bedrock-runtime", **kwargs) as client:
                    method = getattr(client, method_name, None)
                    if not method:
                        raise HTTPException(status_code=404, detail=f"Operation {operation} not found")
                    
                    response = await method(**body)
                    
                    # Extract Usage
                    input_tokens = 0
                    output_tokens = 0
                
                    
                    # Headers for InvokeModel
                    if "ResponseMetadata" in response:
                        headers = response["ResponseMetadata"].get("HTTPHeaders", {})
                        input_tokens = int(headers.get("x-amzn-bedrock-input-token-count", 0))
                        output_tokens = int(headers.get("x-amzn-bedrock-output-token-count", 0))
    
                    # Body usage for Converse
                    if "usage" in response:
                        input_tokens = response["usage"].get("inputTokens", 0)
                        output_tokens = response["usage"].get("outputTokens", 0)
    
                    logger.info(f"Bedrock {operation} Finished | Tokens: {input_tokens + output_tokens} (Input: {input_tokens}, Output: {output_tokens})")
    
                    # Parse body stream if needed (InvokeModel returns 'body' as StreamingBody)
                    if "body" in response and hasattr(response["body"], "read"):
                        response_body = await response["body"].read()
                        logger.info(f"Bedrock Response Body: {response_body.decode('utf-8', errors='replace')}")
                        return Response(content=response_body, media_type="application/json")
                    
                    # Check for outputText/output/etc in standard response
                    logger.info(f"Bedrock Response Output: {json.dumps(response, default=str)}")
    
                    # Clean ResponseMetadata from JSON response if we want pure data, but keeping it is fine.
                    # Remove non-serializable objects
                    clean_response = {k: v for k, v in response.items() if k != "body"}
                    return JSONResponse(content=clean_response)
        
        except Exception as e:
            logger.error(f"Bedrock Error ({operation}): {e}")
            raise HTTPException(status_code=500, detail=str(e))

@router.post("/agent-runtime/{operation}")
async def bedrock_agent_runtime(operation: str, request: Request):
    """
    Handles Bedrock Agent Runtime: Retrieve, RetrieveAndGenerate
    """
    body = await request.json()
    logger.info(f"Bedrock Agent Input ({operation}): {json.dumps(body)}")
    
    creds = await get_credentials()
    kwargs = {"region_name": REGION}
    if creds:
        kwargs.update({
            "aws_access_key_id": creds["AccessKeyId"],
            "aws_secret_access_key": creds["SecretAccessKey"],
            "aws_session_token": creds["SessionToken"]
        })

    async with session.client("bedrock-agent-runtime", **kwargs) as client:
        method = getattr(client, operation_to_method(operation), None)
        if not method:
             raise HTTPException(status_code=404, detail=f"Operation {operation} not found")

        try:
            # We assume non-streaming for agent runtime in this snippet unless specified
            response = await method(**body)
            # Log usage? Agent runtime doesn't always return token usage in headers straightforwardly.
            # We'll log simplified info.
            logger.info(f"Bedrock Agent {operation} Finished")
            logger.info(f"Bedrock Agent Output: {json.dumps(response, default=str)}")
            
            clean_response = {k: v for k, v in response.items() if k != "body"}
            return JSONResponse(content=clean_response)
        except Exception as e:
            logger.error(f"Bedrock Agent Error ({operation}): {e}")
            raise HTTPException(status_code=500, detail=str(e))


def operation_to_method(op: str) -> str:
    """Converts PascalCase URL param to snake_case method name if needed, 
    but boto3 mostly uses snake_case.
    Input from user might be 'InvokeModel' (Pascal) which maps to `invoke_model` (snake) in python.
    """
    # Simple conversion: ConverseStream -> converse_stream
    import re
    # Convert PascalCase to snake_case
    snake = re.sub(r'(?<!^)(?=[A-Z])', '_', op).lower()
    # Convert kebab-case to snake_case
    return snake.replace("-", "_")


async def bedrock_stream_generator(body, method_name, client_kwargs, operation):
    """
    Yields events from Bedrock stream and logs usage from metadata events.
    Manages client context to prevent premature closure.
    """
    input_tokens = 0
    output_tokens = 0
    output_text = ""
    
    try:
        async with session.client("bedrock-runtime", **client_kwargs) as client:
            method = getattr(client, method_name, None)
            if not method:
                yield json.dumps({"error": f"Operation {operation} not found"}) + "\n"
                return

            response = await method(**body)
            stream = response.get("body") if method_name == "invoke_model_with_response_stream" else response.get("stream")

            async for event in stream:
                # Yield the event as a JSON line
                yield json.dumps(serialize_event(event)) + "\n"
                
                # Check for usage
                # InvokeModelWithResponseStream often has internal structure dependent on model
                # ConverseStream has explicit 'metadata' event
                if "metadata" in event:
                usage = event["metadata"].get("usage", {})
                input_tokens = usage.get("inputTokens", 0)
                output_tokens = usage.get("outputTokens", 0)
            
            # For InvokeModelWithResponseStream, usage might be in 'internalServerException' metadata or similar?
            # Actually, standard InvokeModel stream usually doesn't send explicit usage event for all models, 
            # but for some like Claude 3 it does in the final event or as a specific chunk.
            
            # Accumulate text for logging
            if "chunk" in event:
                 chunk = event["chunk"]
                 if "bytes" in chunk:
                     try:
                         data = json.loads(chunk["bytes"])
                         # Common formats: 'outputText', 'completion', 'delta'
                         if "outputText" in data: output_text += data["outputText"]
                         elif "completion" in data: output_text += data["completion"]
                         elif "delta" in data and "text" in data["delta"]: output_text += data["delta"]["text"] # Claude
                     except:
                         pass
            elif "contentBlockDelta" in event: # ConverseStream
                 delta = event["contentBlockDelta"].get("delta", {})
                 if "text" in delta:
                     output_text += delta["text"]

    except Exception as e:
        logger.error(f"Streaming Error: {e}")
        yield json.dumps({"error": str(e)}) + "\n"
    
    if input_tokens + output_tokens > 0:
        logger.info(f"Bedrock Stream Finished | Tokens: {input_tokens + output_tokens} (Input: {input_tokens}, Output: {output_tokens})")
    
    logger.info(f"Bedrock Stream Output: {output_text}")

def serialize_event(event):
    """Helper to handle bytes in event dictionary."""
    new_event = {}
    for k, v in event.items():
        if isinstance(v, bytes):
            new_event[k] = v.decode('utf-8') # Decode payload bytes
        elif isinstance(v, dict):
            new_event[k] = serialize_event(v)
        else:
            new_event[k] = v
    return new_event
