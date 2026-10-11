"""Regenerate survey-shapefile.zip with a WGS84 polygon and its sidecars."""

import io
import zipfile
from pathlib import Path

import shapefile
from pyproj import CRS


def main():
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON) as writer:
        writer.field("name", "C")
        writer.field("category", "C")
        writer.poly(
            [
                [
                    (77.5900, 12.9700),
                    (77.5900, 12.9710),
                    (77.5910, 12.9710),
                    (77.5910, 12.9700),
                    (77.5900, 12.9700),
                ]
            ]
        )
        writer.record("Survey parcel", "parcel")
    destination = Path(__file__).with_name("survey-shapefile.zip")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for extension, data in {
            ".shp": shp.getvalue(),
            ".shx": shx.getvalue(),
            ".dbf": dbf.getvalue(),
            ".prj": CRS.from_epsg(4326).to_wkt().encode(),
            ".cpg": b"UTF-8",
        }.items():
            archive.writestr("survey" + extension, data)
    print(destination)


if __name__ == "__main__":
    main()
