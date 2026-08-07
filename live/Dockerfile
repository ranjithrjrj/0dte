FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY *.json ./
COPY .env.example .env.example

ENV PYTHONUNBUFFERED=1
ENV PAPER_RESULTS_DIR=/app/paper_data
CMD ["python", "run.py", "live"]
