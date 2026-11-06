FROM python:3.12-slim
RUN pip install --no-cache-dir --root-user-action=ignore "mcp==2.3.0" tzdata
WORKDIR /app
COPY server.py .
USER nobody
EXPOSE 8000
CMD ["python", "server.py"]
