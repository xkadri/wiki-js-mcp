FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt mcp-proxy

COPY src ./src

RUN mkdir -p /app/data /app/logs

ENV WIKIJS_MCP_DB=/app/data/wikijs_mappings.db
ENV LOG_FILE=/app/logs/wikijs_mcp.log

EXPOSE 8080

CMD ["mcp-proxy", "--host", "0.0.0.0", "--port", "8080", "--pass-environment", "--", "python", "src/wiki_mcp_server.py"]
