FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code + knowledge files (.dockerignore keeps secrets and local state out)
COPY . .

# Chainlit, which replaced the Streamlit app (app.py). docker-compose.yml
# publishes it on the host's 8501, the address users already have.
EXPOSE 8000

# Must listen on 0.0.0.0 so Docker/EC2 can reach it
CMD ["chainlit", "run", "chainlit_app.py", "--host", "0.0.0.0", "--port", "8000", "--headless"]
