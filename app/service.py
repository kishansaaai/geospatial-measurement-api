"""Orchestrate temporary upload storage, parsing, measurement, and persistence."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi import UploadFile

from app.config import Settings
from app.errors import FileError
from app.measurements import measure
from app.readers import read_kml, read_shapefile, safe_filename
from app.schemas import FeatureResult, FileInfo
from app.storage import Repository


def process_upload(upload: UploadFile, settings: Settings, repository: Repository) -> FileInfo:
    filename = safe_filename(upload.filename or "")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".zip", ".kml"}:
        raise FileError("UNSUPPORTED_FORMAT", "Upload a .kml file or a .zip containing one Shapefile.", 415)
    with TemporaryDirectory(prefix="geo-upload-") as temp:
        path = Path(temp) / ("upload" + suffix)
        size = 0
        with path.open("wb") as destination:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise FileError("UPLOAD_TOO_LARGE", "The file exceeds the upload limit.", 413)
                destination.write(chunk)
        if not size:
            raise FileError("EMPTY_FILE", "The uploaded file is empty.", 400)
        dataset = read_kml(path, settings) if suffix == ".kml" else read_shapefile(path, settings)
        crs_label = dataset.crs.to_string()
        features = [
            FeatureResult(
                index=index,
                feature_id=feature.feature_id,
                geometry_type=feature.geometry_type,
                geometry=feature.geometry,
                properties=feature.properties,
                crs=crs_label,
                measurement=measure(feature, dataset.crs),
            )
            for index, feature in enumerate(dataset.features)
        ]
        measured = sum(feature.measurement.status == "MEASURED" for feature in features)
        problems = sum(
            feature.measurement.status
            in {
                "INVALID_GEOMETRY",
                "UNSUPPORTED",
                "MEASUREMENT_ERROR",
            }
            for feature in features
        )
        warnings = []
        if problems:
            warnings.append(f"{problems} feature(s) could not be measured; inspect their measurement status.")
        if not features:
            warnings.append("The dataset contains no features.")
        info = FileInfo(
            id=uuid4(),
            filename=filename,
            format=dataset.format,
            feature_count=len(features),
            crs=crs_label,
            crs_wkt=dataset.crs.to_wkt(),
            status="COMPLETED",
            uploaded_at=datetime.now(timezone.utc),
            size_bytes=size,
            measured_feature_count=measured,
            warnings=warnings,
        )
        repository.save(info, features)
        return info
