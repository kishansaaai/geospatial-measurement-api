# Geospatial File Measurement API

A FastAPI backend that accepts KML files and zipped Shapefiles, extracts features, and returns polygon areas in square metres and line lengths in metres. File metadata and feature results are stored in SQLite and remain available after a restart.

## Setup

Use Python 3.12 or later. Python 3.12 is used by the Docker image and CI. No desktop GIS application or system GDAL installation is required.

```sh
git clone https://github.com/kishansaaai/geospatial-measurement-api.git
cd geospatial-measurement-api
python -m venv .venv
```

On Windows PowerShell, run these commands without activating the environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On Linux or macOS:

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open [Swagger UI](http://127.0.0.1:8000/docs) to upload a file and try every endpoint. [ReDoc](http://127.0.0.1:8000/redoc) and the [OpenAPI schema](http://127.0.0.1:8000/openapi.json) are also available. SQLite is created automatically at `data/measurements.sqlite3` during application startup.

### Docker

```sh
docker compose up --build
```

The service is available at the same local URL. A named volume keeps the database across container restarts. The container runs as a non-root user and exposes a health check. Copy `.env.example` to `.env` to change the Compose limits. Local Python execution reads operating-system environment variables and does not automatically load `.env`.

## Accepted files

- **KML:** `.kml`, KML 2.2, legacy KML 2.1, or namespace-free KML. Each Placemark becomes a feature. Coordinates follow KML's longitude, latitude, optional altitude order in WGS84.
- **Shapefile:** `.zip` containing exactly one matching `.shp`, `.shx`, `.dbf`, and `.prj` dataset. Matching is case-insensitive and files may be inside a folder. An optional `.cpg` supplies the DBF encoding; UTF-8 is the default.

The Shapefile `.prj` is mandatory because guessing a CRS from numeric coordinates can produce plausible but incorrect results. KMZ, GeoJSON, loose `.shp` files, and ZIPs containing several Shapefiles are not accepted. The example KML contains a polygon, a line, and a point; the example ZIP contains one polygon.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/files/` | Upload and process a multipart `file`; returns 201 and file metadata |
| GET | `/api/files/{id}/` | Retrieve saved file metadata |
| GET | `/api/files/{id}/measurements/` | Retrieve paginated features and measurements |
| GET | `/health` | Check application and database availability |

Uploads are processed synchronously. A successful response means parsing and persistence have finished. `Location` points to the file-information endpoint. `COMPLETED` describes file processing; individual features may still have an invalid, unsupported, or empty geometry.

### Upload

The commands below work in PowerShell with `curl.exe`. On Linux or macOS, use `curl` instead.

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/files/ -F "file=@examples/survey.kml"
curl.exe -X POST http://127.0.0.1:8000/api/files/ -F "file=@examples/survey-shapefile.zip"
```

An example metadata response is saved in `examples/upload-response.json`. It contains the UUID, original filename, format, feature count, source CRS and WKT, status, UTC timestamp, upload size, measured feature count, and any dataset warnings. Generated IDs and timestamps differ on each upload.

### File information

Replace the example UUID with the `id` returned by your upload:

```powershell
curl.exe http://127.0.0.1:8000/api/files/00000000-0000-4000-8000-000000000001/
```

This returns the same metadata as the upload response. A valid but unknown UUID returns 404; an invalid UUID returns 422.

### Measurements

```powershell
curl.exe "http://127.0.0.1:8000/api/files/00000000-0000-4000-8000-000000000001/measurements/?offset=0&limit=100"
```

The response contains `file_id`, `total`, `offset`, `limit`, and `features`. Offset starts at zero. Limit defaults to 100 and must be between 1 and 1,000. Features are returned in source order; an offset beyond the end returns an empty list. `examples/measurements-response.json` contains a complete example response.

Each feature includes:

- `index`: zero-based position, unique within the file.
- `feature_id`: KML Placemark ID when present, otherwise the source index. Source IDs may repeat; use `index` for unambiguous identification.
- `geometry_type` and `geometry`: GeoJSON-style geometry in the **source CRS**, rather than a reprojected copy. Shapefile geometry is exported through PyShp; KML altitude is retained where supplied. These objects may use a projected CRS and are not standalone RFC 7946 GeoJSON.
- `crs`: the source CRS. The file metadata also provides its full WKT.
- `properties`: DBF attributes, or KML name, description, and ExtendedData. Date attributes are ISO-formatted strings.
- `measurement`: status, area or length, the exact measurement projection, message, and warnings. Inapplicable measurements are `null`.

| Geometry | Result |
| --- | --- |
| Polygon or MultiPolygon | `MEASURED`, `area_m2`; holes are subtracted and parts are summed |
| LineString or MultiLineString | `MEASURED`, `length_m`; parts are summed |
| Point or MultiPoint | `NO_MEASUREMENT`; no area or length |
| Null or empty geometry | `EMPTY_GEOMETRY` |
| Mixed GeometryCollection, KML Track or Model, Shapefile MultiPatch | `UNSUPPORTED` |
| Malformed feature or self-intersecting polygon | `INVALID_GEOMETRY` |
| Projection failure or measurement scope exceeded | `MEASUREMENT_ERROR` |

Homogeneous KML MultiGeometry elements become MultiPoint, MultiLineString, or MultiPolygon. Mixed collections retain their geometry but are not measured. Native unsupported KML and Shapefile types retain their type and properties; their GeoJSON geometry is `null`. One failed feature does not prevent valid features from being measured.

### Errors

Expected errors use this envelope:

```json
{
  "error": {
    "code": "MISSING_SIDECARS",
    "message": "Missing Shapefile components: .prj"
  }
}
```

| Status | Typical cause |
| --- | --- |
| 400 | Empty upload or invalid filename |
| 404 | Unknown file ID |
| 413 | Upload, archive expansion, feature, or coordinate budget exceeded |
| 415 | Unsupported filename extension |
| 422 | Invalid KML, corrupt Shapefile, missing sidecars, invalid CRS, unsafe ZIP, or invalid request parameters |

Rejected datasets are not persisted. Successfully parsed datasets and their per-feature results are committed atomically. Originals and extracted sidecars are temporary and are removed after processing.

## Architecture

```text
app/
  main.py           Application factory, routes, OpenAPI, error responses
  config.py         Environment settings and resource limits
  middleware.py     Bounded body spooling before multipart parsing
  schemas.py        Typed file, feature, measurement, and pagination responses
  readers.py        Safe KML and Shapefile readers and attribute normalization
  measurements.py   CRS transformation, geometry checks, and metric calculation
  service.py        Upload-to-result processing flow
  storage.py        SQLite repository and atomic file/feature transactions
tests/              API, reader, measurement, and middleware regression tests
examples/           Ready-to-upload datasets and example JSON responses
```

### File processing flow

1. Bound the request body before multipart parsing, including requests without Content-Length. Body spooling moves to disk after 1 MiB.
2. Validate the filename, extension, and actual file size. Copy the upload to a temporary directory under a generated filename.
3. Parse KML with DTDs, entities, and external resources disabled, or validate the ZIP and select one matching Shapefile dataset. No archive-supplied path is used as an extraction destination.
4. Extract source IDs, attributes, geometry, and CRS, subject to feature and coordinate budgets.
5. Calculate a measurement or a per-feature status, then commit metadata and results in one transaction.
6. Remove all temporary upload components. Later GET requests read the saved results without repeating calculations.

Synchronous route functions run through FastAPI's thread pool so parsing and coordinate calculations do not run directly on the event loop. SQLite connections are scoped to each repository operation, with foreign keys, a 30-second busy timeout, and WAL mode.

### Measurements and CRS handling

The service never measures latitude/longitude degrees as metres. It transforms source coordinates to WGS84 using `always_xy=True`, then chooses a metre-based projection centred on each feature. Projected input is also reprojected; measuring Web Mercator coordinates directly would produce distorted ground areas.

- **Area:** local ellipsoidal Lambert Azimuthal Equal Area (`+proj=laea`).
- **Length:** local ellipsoidal Azimuthal Equidistant (`+proj=aeqd`). This preserves distances from its centre; arbitrary line lengths remain a local projection approximation.
- **Centre:** circular mean longitude and mean latitude of the feature vertices. Circular longitude handles small features crossing the date line.
- **Edges:** source vertices are transformed to WGS84 and consecutive vertices are interpreted as shortest geodesic edges. Segments are subdivided to at most 5 km before projection, reducing chord error. Source geometry in the response is not densified.
- **Units:** area is square metres and length is metres even when input uses feet or other projected units. Measurements are horizontal; altitude is ignored and a warning is returned for measured 3D geometry.

Features with vertices more than 1,000 km from their projection centre return `MEASUREMENT_ERROR`, rather than silently applying this local strategy to global geometry. Lines extending beyond 100 km receive a distortion warning. Projected geometry is checked for validity after transformation, which also handles ordinary date-line crossings. Invalid polygons are not repaired automatically because repair can change their meaning.

This strategy is intended for local and regional datasets. It is not a certified surveying calculation, a terrain distance, or a treatment of global or greater-than-hemisphere polygons. For projected datasets, straight source-grid edges become geodesic edges between transformed vertices; add source vertices when the intended boundary follows a different curve. Datum accuracy depends on the source CRS and available PROJ transformation resources. Small local examples are tested against independent WGS84 geodesic values; that does not establish one accuracy guarantee for all datasets.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `GEO_DATABASE_PATH` | `data/measurements.sqlite3` | Persistent database location |
| `GEO_MAX_UPLOAD_BYTES` | 20 MiB | Actual uploaded file limit |
| `GEO_MAX_ARCHIVE_BYTES` | 100 MiB | Aggregate declared ZIP expansion limit |
| `GEO_MAX_ARCHIVE_MEMBERS` | 100 | Maximum ZIP entries |
| `GEO_MAX_FEATURES` | 10,000 | Maximum features per dataset |
| `GEO_MAX_COORDINATES` | 500,000 | Maximum input coordinates per dataset |

The multipart request body has a separate limit of the file budget plus 1 MiB for headers and form framing. ZIP members with a compression ratio above 200 are rejected, as are traversal paths, absolute paths, duplicates, symlinks, and encrypted entries. Measurement densification has a separate cap of 100,000 generated coordinates per feature. All configurable numeric budgets must be positive.

Example local configuration in PowerShell:

```powershell
$env:GEO_DATABASE_PATH = "data/custom.sqlite3"
$env:GEO_MAX_UPLOAD_BYTES = "10485760"
```

## Tests

On Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q -W error
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

Use `.venv/bin/python` for the same commands on Linux and macOS. CI runs these checks on Windows and Ubuntu with Python 3.12. A separate CI job builds the Docker image and performs health and upload checks.

Tests exercise both formats, metadata and attributes, persistence after restart, holes and multipart geometry, projected metres and feet, geographic metric accuracy, polar and date-line cases, invalid geometry, unsupported types, pagination, XML entity rejection, ZIP traversal and expansion checks, corrupt input, and request limits.

## Design decisions

**FastAPI and Pydantic:** a small service benefits from typed responses, input validation, and generated OpenAPI without the additional configuration of Django and DRF. Django would be a good alternative if the application required an admin interface or a larger domain model.

**PyShp and a focused KML reader:** avoid a GDAL installation for the two requested input formats. GeoPandas with Fiona or Pyogrio would support more drivers and richer vector operations, at the cost of a larger dependency stack. The KML reader deliberately handles geometry, Placemark attributes, and ExtendedData; styles, network links, and overlays are outside its scope and are never fetched.

**Local projections:** choose equal-area and distance projections according to the requested measurement. A single fixed UTM zone would be convenient for one region but would not work well for files from different zones or polar regions. Direct ellipsoidal geodesic measurements are another useful option, but the requirement explicitly asks for a projected measurement flow.

**SQLite:** persistent, transactional storage with no separate database service for local evaluation. Feature rows support indexed pagination and avoid loading every result on each GET. PostgreSQL and PostGIS are the next step for higher concurrency and spatial queries.

**Synchronous processing:** keeps the upload contract easy to use and makes results immediately retrievable. Feature and file limits constrain individual requests, but this does not provide a job queue, a hard processing timeout, or a global concurrency budget.

**Preserve source data and expose failures:** avoid silent CRS assumptions or topology repair. Consumers can inspect individual results and decide whether a feature needs correction.

## Learning

The implementation illustrates why CRS and units are part of the measurement contract: longitude and latitude are angular quantities, while a projected CRS may use metres or feet and may still distort ground measurements. Separate source and measurement CRSs make these choices visible to clients.

It also demonstrates that a geospatial dataset is more than a file extension. Shapefiles rely on matching sidecars, ring topology and attributes need explicit handling, and XML and ZIP uploads need bounded parsing. Testing against independent geodesic calculations catches unit and projection errors that ordinary endpoint success tests miss.

Separating readers, measurements, orchestration, and storage keeps those concerns testable and makes it easier to add another format or replace the persistence layer.

## Future scope

- Add background jobs, progress states, cancellation, processing timeouts, and bounded concurrency for larger uploads.
- Add PostgreSQL/PostGIS, spatial querying, database migrations, retention policies, and deletion endpoints.
- Add authentication, ownership checks, rate limits, TLS termination, and request limits/timeouts at a reverse proxy before exposing a multi-user deployment.
- Support KMZ, GeoJSON, GeoPackage, multiple layers, and explicitly chosen CRS overrides for missing or incorrect metadata.
- Add configurable measurement methods, projection quality thresholds, geodesic cross-checks, transformation accuracy reporting, and terrain-aware measurements.
- Add structured request logging, metrics, tracing, load tests, and dataset fuzzing.

## References

- [FastAPI file uploads](https://fastapi.tiangolo.com/tutorial/request-files/)
- [PyProj coordinate transformers and axis order](https://pyproj4.github.io/pyproj/stable/api/transformer.html)
- [PROJ Lambert Azimuthal Equal Area](https://proj.org/en/stable/operations/projections/laea.html)
- [PROJ Azimuthal Equidistant](https://proj.org/en/stable/operations/projections/aeqd.html)
- [Shapely coordinate transformations](https://shapely.readthedocs.io/en/stable/reference/shapely.transform.html)
