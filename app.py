import sys
import os
import tempfile
import certifi
from dotenv import load_dotenv
import numpy as np
from fastapi import FastAPI, File, UploadFile, Request, BackgroundTasks, HTTPException
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
from networksecurity.utils.main_utils.utils import load_object, read_yaml_file
from networksecurity.utils.ml_utils.model.estimator import NetworkModel
from networksecurity.constant.training_pipeline import (
    DATA_INGESTION_COLLECTION_NAME,
    DATA_INGESTION_DATABASE_NAME,
    TRAINING_BUCKET_NAME,
    SCHEMA_FILE_PATH,
    TARGET_COLUMN,
)

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


def get_schema_columns():
    schema = read_yaml_file(SCHEMA_FILE_PATH)
    columns = []

    for column in schema.get("columns", []):
        if isinstance(column, dict):
            columns.extend(column.keys())
        else:
            columns.append(column)

    return columns


def load_model_artifacts():
    model_paths = {
        "preprocessor": "final_model/preprocessor.pkl",
        "model": "final_model/model.pkl",
    }
    missing_artifacts = [
        path for path in model_paths.values() if not os.path.isfile(path)
    ]
    if missing_artifacts:
        raise FileNotFoundError(
            "Missing model artifacts: " + ", ".join(missing_artifacts)
        )

    app.state.preprocessor = load_object(model_paths["preprocessor"])
    app.state.final_model = load_object(model_paths["model"])
    app.state.network_model = NetworkModel(
        preprocessor=app.state.preprocessor,
        model=app.state.final_model,
    )


def replace_training_dataframe(dataframe: pd.DataFrame):
    if dataframe.empty:
        raise ValueError("Uploaded CSV is empty.")

    required_columns = get_schema_columns()
    missing_columns = [
        column for column in required_columns if column not in dataframe.columns
    ]
    if missing_columns:
        raise ValueError(
            "Training CSV is missing required columns: " + str(missing_columns)
        )

    dataframe = dataframe[required_columns].copy()
    dataframe = dataframe.replace({"na": np.nan})

    invalid_values = {}
    for column in required_columns:
        original_values = dataframe[column]
        numeric_values = pd.to_numeric(original_values, errors="coerce")
        invalid_mask = original_values.notna() & numeric_values.isna()
        if invalid_mask.any():
            invalid_values[column] = original_values[invalid_mask].head(5).tolist()
        dataframe[column] = numeric_values

    if invalid_values:
        raise ValueError(
            "CSV contains non-numeric values in numeric columns: "
            + str(invalid_values)
        )

    if dataframe[TARGET_COLUMN].isna().any():
        raise ValueError("CSV contains missing Result target values.")

    allowed_targets = {-1, 0, 1}
    unexpected_targets = sorted(
        set(dataframe[TARGET_COLUMN].unique()) - allowed_targets
    )
    if unexpected_targets:
        raise ValueError(
            "Result contains unsupported labels: " + str(unexpected_targets)
        )

    dataframe = dataframe.mask(dataframe.isna(), np.nan)
    records = dataframe.to_dict(orient="records")
    app.state.collection.delete_many({})
    result = app.state.collection.insert_many(records)

    return {
        "inserted_count": len(result.inserted_ids),
        "total_rows_in_collection": app.state.collection.count_documents({}),
    }


@app.on_event("startup")
async def startup_event():
    """Initialize environment, MongoDB connection, and model artifacts on app startup."""
    ca = certifi.where()
    load_dotenv()
    mongo_db_url = os.getenv("MONGO_DB_URL")

    if not mongo_db_url:
        raise RuntimeError("MONGO_DB_URL is not set in environment variables.")

    try:
        client = pymongo.MongoClient(mongo_db_url, tlsCAFile=ca)
        app.state.mongo_client = client
        app.state.database = client[DATA_INGESTION_DATABASE_NAME]
        app.state.collection = app.state.database[DATA_INGESTION_COLLECTION_NAME]
        print("MongoDB connection successful")
    except Exception as e:
        print("MongoDB connection failed:", e)
        raise e

    try:
        load_model_artifacts()
        print("Model artifacts loaded successfully")
    except Exception as e:
        print("Failed to load model artifacts:", e)
        raise e


# ------------------- Routes -------------------

@app.get("/", tags=["authentication"])
async def index(request: Request):
    """Render the application dashboard."""
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {}
    )


@app.get("/health", tags=["monitoring"])
async def health_check():
    """Health check endpoint."""
    mongo_ready = False
    if hasattr(app.state, "mongo_client"):
        try:
            app.state.mongo_client.admin.command("ping")
            mongo_ready = True
        except Exception:
            mongo_ready = False

    model_ready = hasattr(app.state, "network_model")

    return {
        "status": "ok" if mongo_ready and model_ready else "degraded",
        "mongodb": "ready" if mongo_ready else "unavailable",
        "model": "ready" if model_ready else "unavailable",
    }


@app.get("/train", tags=["training"])
async def train_route():
    """Run model training pipeline."""
    try:
        train_pipeline = TrainingPipeline()
        train_pipeline.run_pipeline()
        load_model_artifacts()
        return Response(content="Training is successful", media_type="text/plain")
    except Exception as e:
        raise NetworkSecurityException(e, sys)


@app.post("/data/upload", tags=["data"])
async def upload_training_data(file: UploadFile = File(...)):
    """Replace the MongoDB training collection with a validated labeled CSV."""
    try:
        dataframe = pd.read_csv(file.file)
        replace_summary = replace_training_dataframe(dataframe)

        return {
            "status": "success",
            **replace_summary,
            "message": "Existing MongoDB data was deleted and replaced with the uploaded rows.",
        }
    except (ValueError, pd.errors.ParserError, pd.errors.EmptyDataError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise NetworkSecurityException(e, sys)


@app.get("/data/download", tags=["data"])
async def download_training_data(background_tasks: BackgroundTasks):
    """Export all current MongoDB training data as a CSV file."""
    temporary_path = None
    try:
        records = list(app.state.collection.find({}, {"_id": 0}))
        if not records:
            raise HTTPException(status_code=404, detail="MongoDB collection is empty.")

        dataframe = pd.DataFrame(records)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", prefix="network_data_", delete=False
        ) as temporary_file:
            temporary_path = temporary_file.name

        dataframe.to_csv(temporary_path, index=False)
        background_tasks.add_task(os.remove, temporary_path)

        return FileResponse(
            temporary_path,
            filename="network_training_data.csv",
            media_type="text/csv",
            background=background_tasks,
        )
    except HTTPException:
        raise
    except Exception as e:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)
        raise NetworkSecurityException(e, sys)


@app.post("/predict", tags=["prediction"])
async def predict_route(request: Request, file: UploadFile = File(...)):
    """Predict network security threat from uploaded CSV file."""
    try:
        df = pd.read_csv(file.file)

        if df.empty:
            raise ValueError("Uploaded CSV is empty.")

        schema = read_yaml_file(SCHEMA_FILE_PATH)

        raw_columns = schema.get("columns", [])
        feature_columns = []

        for col in raw_columns:
            if isinstance(col, dict):
                feature_columns.extend(col.keys())
            else:
                feature_columns.append(col)

        feature_columns = [col for col in feature_columns if col != "Result"]

        if "Result" in df.columns:
            df = df.drop(columns=["Result"])

        missing_columns = [col for col in feature_columns if col not in df.columns]
        if missing_columns:
            raise ValueError(
                f"Uploaded CSV is missing required feature columns: {missing_columns}"
            )

        df = df[feature_columns]

        y_pred = app.state.network_model.predict(df)
        df["predicted_column"] = y_pred

        output_dir = "prediction_output"
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "output.csv")
        df.to_csv(output_path, index=False)

        prediction_response = {
            "status": "success",
            "rows": len(df),
            "prediction_count": len(y_pred),
            "download_link": "/download",
            "predictions": y_pred.tolist(),
        }

        accept_header = request.headers.get("accept", "")
        if "application/json" in accept_header.lower():
            return prediction_response

        table_html = df.head(50).to_html(classes="table table-striped", index=False)

        return templates.TemplateResponse(
            request,
            "table.html",
            {
                "table": table_html,
                "download_link": "/download",
                "prediction_count": len(y_pred),
            }
        )

    except HTTPException:
        raise
    except (ValueError, pd.errors.ParserError, pd.errors.EmptyDataError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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
