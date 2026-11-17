FROM python:3.11-slim
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend /app/backend
COPY frontend /app/frontend
COPY seed /app/seed
COPY README.md APPROACH.md DECISIONS.md /app/
RUN mkdir -p /app/data /app/submission
ENV DATA_DIR=/app/data SUBMISSION_DIR=/app/submission
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1
CMD ["uvicorn","backend.main:app","--host","0.0.0.0","--port","8000"]
