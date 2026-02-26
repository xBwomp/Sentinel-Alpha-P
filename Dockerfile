FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY main.py /app/main.py

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD grep -aq "main.py" /proc/1/cmdline || exit 1

CMD ["python", "main.py"]
