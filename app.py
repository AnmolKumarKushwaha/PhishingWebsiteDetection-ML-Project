import sys
import os
import certifi
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from starlette.responses import RedirectResponse
from uvicorn import run as app_run
from fastapi.templating import Jinja2Templates
import pandas as pd
import pymongo

from networksecurity.exception.exception import NetworkSecurityException
from networksecurity.logging.logger import logging
from networksecurity.pipeline.training_pipeline import TrainingPipeline
from networksecurity.utils.main_utils.utils import load_object
from networksecurity.utils.ml_utils.model.estimator import NetworkModel
from networksecurity.constant.training_pipeline import (
    DATA_INGESTION_COLLECTION_NAME,
    DATA_INGESTION_DATABASE_NAME,
    TRAINING_BUCKET_NAME,
)

# ------------------- Load Environment -------------------
ca = certifi.where()
load_dotenv()
mongo_db_url = os.getenv("MONGO_DB_URL")
print("MongoDB URL:", mongo_db_url)

# ------------------- DB Connection -------------------
try:
    client = pymongo.MongoClient(mongo_db_url, tlsCAFile=ca)
    database = client[DATA_INGESTION_DATABASE_NAME]
    collection = database[DATA_INGESTION_COLLECTION_NAME]
    print("MongoDB connection successful")
except Exception as e:
    print("MongoDB connection failed:", e)
    raise e

# ------------------- FastAPI Setup -------------------
app = FastAPI(
    debug=True,
    title="Network Security App",
    description="API for training and predicting network security threats",
    version="1.0",
    root_path=os.getenv("RENDER_EXTERNAL_URL", "")  # Fix for Render reverse proxy
)

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="./templates")

# ------------------- Routes -------------------

@app.get("/", tags=["authentication"])
async def index():
    """Redirect to Swagger UI."""
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["monitoring"])
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/train", tags=["training"])
async def train_route():
    """Run model training pipeline."""
    try:
        train_pipeline = TrainingPipeline()
        train_pipeline.run_pipeline()
        return Response(content="Training is successful", media_type="text/plain")
    except Exception as e:
        raise NetworkSecurityException(e, sys)


@app.post("/predict", tags=["prediction"])
async def predict_route(request: Request, file: UploadFile = File(...)):
    """Predict network security threat from uploaded CSV file."""
    try:
        df = pd.read_csv(file.file)
        preprocessor = load_object("final_model/preprocessor.pkl")
        final_model = load_object("final_model/model.pkl")
        network_model = NetworkModel(preprocessor=preprocessor, model=final_model)

        y_pred = network_model.predict(df)
        df["predicted_column"] = y_pred

        output_dir = "prediction_output"
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "output.csv")
        df.to_csv(output_path, index=False)

        table_html = df.head(50).to_html(classes="table table-striped", index=False)

        return templates.TemplateResponse(
            "table.html",
            {
                "request": request,
                "table": table_html,
                "download_link": "/download"
            }
        )

    except Exception as e:
        raise NetworkSecurityException(e, sys)


@app.get("/download", tags=["prediction"])
async def download_file(background_tasks: BackgroundTasks):
    """Download prediction results as CSV."""
    try:
        output_dir = "prediction_output"
        output_path = os.path.join(output_dir, "output.csv")

        print("Checking file:", os.path.abspath(output_path))

        if not os.path.exists(output_path):
            return Response(
                content="No prediction file found. Please run /predict first.",
                media_type="text/plain",
                status_code=404
            )

        def cleanup():
            try:
                os.remove(output_path)
                if not os.listdir(output_dir):
                    os.rmdir(output_dir)
                print("Deleted output file after download.")
            except Exception as cleanup_error:
                print("Cleanup failed:", cleanup_error)

        background_tasks.add_task(cleanup)

        print("File ready for download.")
        return FileResponse(
            output_path,
            filename="predictions.csv",
            media_type="text/csv",
            background=background_tasks
        )

    except Exception as e:
        print("Error during download:", e)
        return Response(
            content=f"Internal Server Error: {e}",
            media_type="text/plain",
            status_code=500
        )


# ------------------- Run Locally -------------------
if __name__ == "__main__":
    app_run(app, host="0.0.0.0", port=8000)
