# Async AI Proxy

A high-performance, asynchronous proxy server for Azure OpenAI and AWS Bedrock, built with Python 3.14 and `uv`.

## Features

- **Asynchronous Handling**: Built on FastAPI and `uvicorn` for high concurrency.
- **Provider Support**:
    - **Azure OpenAI**: Completions, Chat Completions.
    - **AWS Bedrock**: Runtime (InvokeModel, Converse) and Agent Runtime.
- **Token Logging**: Automatically logs input and output token usage from provider responses.
- **Request/Response Logging**: Logs full request bodies and response text (including accumulated stream content).
- **Streaming Support**: Full support for streaming responses with usage logic.

## Prerequisites

- Python 3.14+
- `uv` package manager

## Installation & Running

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd proxy
   ```

2. **Run the server**:
   ```bash
   uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```
   `uv` will automatically install dependencies defined in `pyproject.toml`.

## Configuration

Copy `.env.example` to `.env` and configure your credentials:

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `AZURE_OPENAI_ENDPOINT` | URL of your Azure OpenAI resource. |
| `AWS_REGION` | AWS Region (default: `eu-central-1`). |
| `AWS_ROLE_ARN` | (Optional) IAM Role ARN to assume for Bedrock calls. |

## Usage

### Azure OpenAI
**Endpoint**: `POST /azure/{path}`

Example:
```bash
curl -X POST "http://localhost:8000/azure/openai/deployments/gpt-4/chat/completions?api-version=2024-02-15-preview" \
     -H "Content-Type: application/json" \
     -H "api-key: YOUR_KEY" \
     -d '{
           "messages": [{"role": "user", "content": "Hello!"}],
           "stream": true
         }'
```

### AWS Bedrock
**Endpoint**: `POST /bedrock/runtime/{operation}` (e.g., `InvokeModel`, `Converse`)

Example (Converse):
```bash
curl -X POST "http://localhost:8000/bedrock/runtime/Converse" \
     -H "Content-Type: application/json" \
     -d '{
           "modelId": "anthropic.claude-3-sonnet-20240229-v1:0",
           "messages": [{"role": "user", "content": [{"text": "Hello"}]}]
         }'
```
