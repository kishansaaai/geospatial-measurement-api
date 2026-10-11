import io
import zipfile

import pytest
import shapefile
from fastapi.testclient import TestClient
from pyproj import CRS

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(database_path=tmp_path / "results.sqlite3")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def kml(geometries, properties=""):
    placemarks = "".join(
        f'<Placemark id="feature-{index}">{properties}{geometry}</Placemark>'
        for index, geometry in enumerate(geometries)
    )
    return (f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{placemarks}</Document></kml>').encode()


def shape_zip(shape_type=shapefile.POLYGON, crs="EPSG:4326", omit=(), prefix="nested/survey", null=False):
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shape_type) as writer:
        writer.field("name", "C")
        writer.field("visited", "D")
        if null:
            writer.null()
        elif shape_type == shapefile.POLYGON:
            # Clockwise exterior for the Shapefile polygon convention.
            writer.poly([[(77, 12), (77, 12.01), (77.01, 12.01), (77.01, 12), (77, 12)]])
        elif shape_type == shapefile.POLYLINE:
            writer.line([[(77, 12), (77.01, 12)]])
        elif shape_type == shapefile.POINT:
            writer.point(77, 12)
        writer.record("survey", "20261011")
    components = {
        ".shp": shp.getvalue(),
        ".shx": shx.getvalue(),
        ".dbf": dbf.getvalue(),
        ".prj": CRS.from_user_input(crs).to_wkt().encode(),
        ".cpg": b"UTF-8",
    }
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        for extension, content in components.items():
            if extension not in omit:
                archive.writestr(prefix + extension, content)
    return result.getvalue()


@pytest.fixture
def upload():
    def perform(client, content, filename="survey.kml"):
        return client.post("/api/files/", files={"file": (filename, content, "application/octet-stream")})

    return perform


@pytest.fixture
def polygon_kml():
    return kml(
        [
            """<Polygon><outerBoundaryIs><LinearRing><coordinates>
        77,12 77.01,12 77.01,12.01 77,12.01 77,12
        </coordinates></LinearRing></outerBoundaryIs></Polygon>"""
        ]
    )
