import io
import stat
import zipfile
from uuid import uuid4

import pytest
import shapefile
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import kml, shape_zip


def results(client, response, **params):
    return client.get(f"/api/files/{response.json()['id']}/measurements/", params=params)


def test_upload_metadata_measurements_and_location(client, upload, polygon_kml):
    response = upload(client, polygon_kml)
    assert response.status_code == 201
    info = response.json()
    assert info["feature_count"] == info["measured_feature_count"] == 1
    assert info["crs"] == "EPSG:4326"
    assert info["status"] == "COMPLETED"
    assert info["format"] == "KML"
    assert client.get(response.headers["location"]).json() == info
    feature = results(client, response).json()["features"][0]
    assert feature["index"] == 0
    assert feature["feature_id"] == "feature-0"
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["crs"] == "EPSG:4326"
    assert feature["measurement"]["area_m2"] > 1_000_000
    assert feature["measurement"]["length_m"] is None


@pytest.mark.parametrize(
    "shape_type,kind",
    [
        (shapefile.POLYGON, "Polygon"),
        (shapefile.POLYLINE, "LineString"),
        (shapefile.POINT, "Point"),
    ],
)
def test_shapefile_formats_and_properties(client, upload, shape_type, kind):
    response = upload(client, shape_zip(shape_type), "survey.zip")
    assert response.status_code == 201, response.text
    feature = results(client, response).json()["features"][0]
    assert feature["geometry_type"] == kind
    assert feature["properties"] == {"name": "survey", "visited": "2026-10-11"}
    assert feature["measurement"]["status"] == ("NO_MEASUREMENT" if kind == "Point" else "MEASURED")


def test_shapefile_null_geometry(client, upload):
    response = upload(client, shape_zip(null=True), "null.zip")
    assert response.status_code == 201
    assert results(client, response).json()["features"][0]["measurement"]["status"] == "EMPTY_GEOMETRY"


def test_kml_multipart_and_unsupported_do_not_abort(client, upload):
    line = "<LineString><coordinates>77,12 77.01,12</coordinates></LineString>"
    point = "<Point><coordinates>77,12</coordinates></Point>"
    response = upload(
        client,
        kml(
            [
                f"<MultiGeometry>{line}{line}</MultiGeometry>",
                f"<MultiGeometry>{line}{point}</MultiGeometry>",
                '<gx:Track xmlns:gx="http://www.google.com/kml/ext/2.2"><gx:coord>77 12 0</gx:coord></gx:Track>',
                point,
            ]
        ),
    )
    assert response.status_code == 201
    features = results(client, response).json()["features"]
    assert [f["measurement"]["status"] for f in features] == [
        "MEASURED",
        "UNSUPPORTED",
        "UNSUPPORTED",
        "NO_MEASUREMENT",
    ]
    assert features[0]["geometry_type"] == "MultiLineString"
    assert response.json()["warnings"]


def test_kml_extended_data_and_empty_feature(client, upload):
    properties = '<name>Survey</name><description>Area</description><ExtendedData><Data name="owner"><value>Example</value></Data><SchemaData><SimpleData name="code">42</SimpleData></SchemaData></ExtendedData>'
    response = upload(client, kml([""], properties))
    feature = results(client, response).json()["features"][0]
    assert feature["properties"] == {
        "name": "Survey",
        "description": "Area",
        "owner": "Example",
        "code": "42",
    }
    assert feature["measurement"]["status"] == "EMPTY_GEOMETRY"


@pytest.mark.parametrize(
    "content,code,status",
    [
        (b"", "EMPTY_FILE", 400),
        (b"<kml>", "INVALID_KML", 422),
        (b"<not-kml/>", "INVALID_KML", 422),
        (b'<!DOCTYPE kml [<!ENTITY x "text">]><kml>&x;</kml>', "INVALID_KML", 422),
    ],
)
def test_bad_kml(client, upload, content, code, status):
    response = upload(client, content)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_malformed_feature_is_reported(client, upload):
    response = upload(
        client,
        kml(
            [
                "<LineString><coordinates>invalid</coordinates></LineString>",
                "<Point><coordinates>181,12</coordinates></Point>",
            ]
        ),
    )
    assert response.status_code == 201
    assert all(
        f["measurement"]["status"] == "INVALID_GEOMETRY" for f in results(client, response).json()["features"]
    )


@pytest.mark.parametrize("extension", [".shp", ".shx", ".dbf", ".prj"])
def test_missing_sidecars(client, upload, extension):
    response = upload(client, shape_zip(omit=(extension,)), "survey.zip")
    assert response.status_code == 422
    assert response.json()["error"]["code"] in {"MISSING_SIDECARS", "INVALID_SHAPEFILE"}


def zip_members(members):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return result.getvalue()


@pytest.mark.parametrize("path", ["../escape.shp", "/absolute.shp", "C:/escape.shp", "..\\escape.shp"])
def test_zip_traversal_is_rejected(client, upload, path):
    response = upload(client, zip_members([(path, b"data")]), "unsafe.zip")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSAFE_ARCHIVE"


def test_zip_symlink_rejected(client, upload):
    member = zipfile.ZipInfo("link.shp")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    response = upload(client, zip_members([(member, b"/etc/passwd")]), "unsafe.zip")
    assert response.json()["error"]["code"] == "UNSAFE_ARCHIVE"


def test_zip_expansion_and_duplicate_paths_rejected(client, upload):
    response = upload(client, zip_members([("bomb.shp", b"0" * 200_000)]), "unsafe.zip")
    assert response.status_code == 413
    response = upload(client, zip_members([("survey.shp", b"x"), ("SURVEY.SHP", b"y")]), "duplicate.zip")
    assert response.json()["error"]["code"] == "UNSAFE_ARCHIVE"


def test_non_zip_and_multiple_datasets(client, upload):
    response = upload(client, b"invalid", "bad.zip")
    assert response.json()["error"]["code"] == "INVALID_SHAPEFILE"
    response = upload(client, zip_members([("one.shp", b"x"), ("two.shp", b"x")]), "two.zip")
    assert response.status_code == 422


def test_unsupported_file_and_missing_form_field(client, upload):
    assert upload(client, b"{}", "file.geojson").status_code == 415
    response = client.post("/api/files/")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_filename_is_sanitized(client, upload, polygon_kml):
    response = upload(client, polygon_kml, "../../survey.kml")
    assert response.json()["filename"] == "survey.kml"


def test_pagination_and_unknown_id(client, upload):
    point = "<Point><coordinates>77,12</coordinates></Point>"
    response = upload(client, kml([point] * 3))
    page = results(client, response, offset=1, limit=1).json()
    assert page["total"] == 3
    assert [f["index"] for f in page["features"]] == [1]
    assert results(client, response, offset=4).json()["features"] == []
    assert results(client, response, limit=1001).status_code == 422
    assert results(client, response, offset=-1).status_code == 422
    assert client.get(f"/api/files/{uuid4()}/").status_code == 404
    assert client.get(f"/api/files/{uuid4()}/measurements/").status_code == 404
    assert client.get("/api/files/not-a-uuid/").status_code == 422


def test_results_survive_app_restart(settings, upload, polygon_kml):
    with TestClient(create_app(settings)) as first:
        response = upload(first, polygon_kml)
        file_id = response.json()["id"]
    with TestClient(create_app(settings)) as second:
        assert second.get(f"/api/files/{file_id}/").status_code == 200
        assert second.get(f"/api/files/{file_id}/measurements/").json()["total"] == 1


def test_resource_limits(tmp_path, upload, polygon_kml):
    settings = Settings(database_path=tmp_path / "limits.sqlite3", max_upload_bytes=32)
    with TestClient(create_app(settings)) as client:
        assert upload(client, polygon_kml).status_code == 413
        assert client.post("/api/files/", content=b"x" * (1024 * 1024 + 33)).status_code == 413
    settings = Settings(database_path=tmp_path / "features.sqlite3", max_features=1)
    with TestClient(create_app(settings)) as client:
        response = upload(client, kml(["", ""]))
        assert response.json()["error"]["code"] == "TOO_MANY_FEATURES"
    settings = Settings(database_path=tmp_path / "coordinates.sqlite3", max_coordinates=1)
    with TestClient(create_app(settings)) as client:
        response = upload(client, polygon_kml)
        assert response.json()["error"]["code"] == "TOO_MANY_COORDINATES"


def test_health_and_openapi(client):
    assert client.get("/health").json() == {"status": "ok"}
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/files/" in paths
    assert "/api/files/{file_id}/measurements/" in paths
