FROM python:3.11-slim

WORKDIR /app

# Install system dependencies if needed
# (Removed gcc and python3-dev as standard wheels are available for the required packages)

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Run the agent
CMD ["python", "main.py"]
