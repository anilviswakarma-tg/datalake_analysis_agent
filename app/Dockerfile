FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code + knowledge files
COPY . .

# Streamlit port
EXPOSE 8501

# Streamlit must listen on 0.0.0.0 so Docker/EC2 can reach it
CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
