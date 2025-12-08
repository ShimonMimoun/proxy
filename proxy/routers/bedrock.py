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

async def get_credentials():
    """
    Returns credentials dictionary. If AWS_ROLE_ARN is set, assumes that role.
    Otherwise returns None (letting boto3 find default credentials).
    """
    if not ROLE_ARN:
        return None
        
    async with session.client("sts", region_name=REGION) as sts:
        resp = await sts.assume_role(
            RoleArn=ROLE_ARN,
            RoleSessionName="ProxySession"
        )
        return resp["Credentials"]

@router.post("/runtime/{operation}")
async def bedrock_runtime(operation: str, request: Request):
    """
    Handles Bedrock Runtime operations: InvokeModel, Converse, etc.
    Expected custom header for model-id if not in body, though typically in path for generic invoke.
    However, aioboto3 client methods take arguments matching the API.
    We receive a JSON body matching the boto3 arguments for the operation.
    """
    body = await request.json()
    
    # Check if this is a streaming operation
    is_stream = operation in ["InvokeModelWithResponseStream", "ConverseStream"]
    
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

    async with session.client("bedrock-runtime", **kwargs) as client:
        method = getattr(client, operation_to_method(operation), None)
        if not method:
             raise HTTPException(status_code=404, detail=f"Operation {operation} not found")
        
        try:
            if is_stream:
                response = await method(**body)
                stream = response.get("body") if operation == "InvokeModelWithResponseStream" else response.get("stream")
                return StreamingResponse(
                    bedrock_stream_generator(stream, operation),
                    media_type="application/json"
                )
            else:
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
    return re.sub(r'(?<!^)(?=[A-Z])', '_', op).lower()


async def bedrock_stream_generator(stream, operation):
    """
    Yields events from Bedrock stream and logs usage from metadata events.
    """
    input_tokens = 0
    output_tokens = 0
    output_text = ""
    
    try:
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
            
                    if "text" in part:
                        output_text += part["text"]
            
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
