FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir \
    coinbase-agentkit \
    pandas \
    statsmodels \
    python-dotenv \
    requests

COPY main.py /app/main.py

CMD ["python", "main.py"]
