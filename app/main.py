"""FastAPI application factory and documented public endpoints."""

import logging
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, File, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import Settings
from app.errors import FileError
from app.middleware import BodyLimitMiddleware
from app.schemas import FileInfo, MeasurementPage
from app.service import process_upload
from app.storage import Repository

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    repository = Repository(settings.database_path)

    @asynccontextmanager
    async def lifespan(application):
        repository.initialize()
        yield

    application = FastAPI(
        title="Geospatial File Measurement API",
        version="1.0.0",
        description="Upload KML or zipped Shapefiles and retrieve feature geometry and horizontal metric measurements.",
        lifespan=lifespan,
    )
    application.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_upload_bytes + 1024 * 1024)
    application.state.repository = repository

    @application.exception_handler(FileError)
    async def file_error_handler(request: Request, error: FileError):
        return JSONResponse({"error": {"code": error.code, "message": error.message}}, error.status_code)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, error: RequestValidationError):
        return JSONResponse(
            {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Check the uploaded file, ID, and query parameters.",
                }
            },
            422,
        )

    @application.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/docs")

    @application.get("/health", tags=["health"])
    def health():
        # Check the persistent store rather than returning a constant success.
        connection = repository.connect()
        try:
            connection.execute("SELECT 1 FROM files LIMIT 1").fetchone()
        finally:
            connection.close()
        return {"status": "ok"}

    @application.post(
        "/api/files/",
        response_model=FileInfo,
        status_code=201,
        tags=["files"],
        responses={
            400: {"description": "Empty or invalid upload"},
            413: {"description": "Resource limit exceeded"},
            415: {"description": "Unsupported file extension"},
            422: {"description": "Invalid dataset, CRS, or archive"},
        },
    )
    def upload_file(
        response: Response, file: Annotated[UploadFile, File(description="KML or zipped Shapefile")]
    ):
        """Process synchronously; per-feature failures are included in the results."""
        try:
            info = process_upload(file, settings, repository)
        finally:
            file.file.close()
        response.headers["Location"] = f"/api/files/{info.id}/"
        logger.info("Processed file %s: %s features", info.id, info.feature_count)
        return info

    def require_file(file_id):
        info = repository.get(str(file_id))
        if info is None:
            raise FileError("NOT_FOUND", "No uploaded file has this ID.", 404)
        return info

    @application.get(
        "/api/files/{file_id}/",
        response_model=FileInfo,
        tags=["files"],
        responses={404: {"description": "Unknown file"}},
    )
    def file_info(file_id: UUID):
        return require_file(file_id)

    @application.get(
        "/api/files/{file_id}/measurements/",
        response_model=MeasurementPage,
        tags=["files"],
        responses={404: {"description": "Unknown file"}},
    )
    def file_measurements(
        file_id: UUID,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ):
        info = require_file(file_id)
        return MeasurementPage(
            file_id=file_id,
            total=info.feature_count,
            offset=offset,
            limit=limit,
            features=repository.features(str(file_id), offset, limit),
        )

    return application


app = create_app()
