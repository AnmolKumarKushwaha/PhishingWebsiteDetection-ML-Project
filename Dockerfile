# 1️⃣ Use a lightweight Python base image
FROM python:3.10-slim

# 2️⃣ Set working directory inside container
WORKDIR /app

# 3️⃣ Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4️⃣ Copy the rest of the app
COPY . /app

# 5️⃣ Expose the FastAPI port
EXPOSE 8000

# 6️⃣ Environment variables (non-sensitive)
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
ENV MONGO_DB_URL="mongodb+srv://anmolkushwaha25807890_db_user:Kush2580@cluster0.arfl0if.mongodb.net/?appName=Cluster0"
ENV MLFLOW_TRACKING_URI="https://dagshub.com/AnmolKumarKushwaha/networksecurity-combined.mlflow"
ENV MLFLOW_TRACKING_USERNAME="AnmolKumarKushwaha"

# ✅ Add these for DagsHub fix
ENV MLFLOW_EXPERIMENTAL_OAUTH2=false
ENV MLFLOW_TRACKING_INSECURE_TLS=true
ENV GIT_PYTHON_REFRESH=quiet


# 7️⃣ Default command to run your FastAPI app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
