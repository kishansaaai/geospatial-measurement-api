"""Read KML and Shapefile ZIPs without trusting archive paths or XML entities."""

import json
import math
import re
import stat
import struct
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

import shapefile
from defusedxml import ElementTree as SafeXML
from defusedxml.common import DefusedXmlException
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.errors import GEOSException
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    mapping,
)

from app.config import Settings
from app.errors import FileError


@dataclass
class RawFeature:
    feature_id: str
    geometry: dict[str, Any] | None
    geometry_type: str | None
    properties: dict[str, Any]
    error: str | None = None


@dataclass
class Dataset:
    crs: CRS
    features: list[RawFeature]
    format: str


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def json_safe(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(item) for item in value]
    return value


def coordinate_count(geometry):
    if not geometry:
        return 0
    if geometry.get("type") == "GeometryCollection":
        return sum(coordinate_count(g) for g in geometry["geometries"])

    def count(value):
        if not value:
            return 0
        if isinstance(value[0], (int, float)):
            return 1
        return sum(count(item) for item in value)

    return count(geometry.get("coordinates", []))


def enforce_budget(features, settings):
    if len(features) > settings.max_features:
        raise FileError("TOO_MANY_FEATURES", "The file exceeds the feature limit.", 413)
    if sum(coordinate_count(f.geometry) for f in features) > settings.max_coordinates:
        raise FileError("TOO_MANY_COORDINATES", "The file exceeds the coordinate limit.", 413)


def kml_coordinates(element):
    coordinates = next((node for node in element.iter() if local_name(node.tag) == "coordinates"), None)
    if coordinates is None or not coordinates.text or not coordinates.text.strip():
        return []
    result = []
    dimension = None
    for token in coordinates.text.split():
        parts = token.split(",")
        if len(parts) not in (2, 3):
            raise ValueError("KML coordinates must have longitude, latitude, and optional altitude.")
        point = tuple(float(part) for part in parts)
        if not all(math.isfinite(value) for value in point):
            raise ValueError("Coordinates must be finite numbers.")
        if not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90):
            raise ValueError("KML longitude or latitude is outside its valid range.")
        if dimension is not None and dimension != len(point):
            raise ValueError("Coordinate dimensions must be consistent within a geometry.")
        dimension = len(point)
        result.append(point)
    return result


GEOMETRY_TAGS = {
    "Point",
    "LineString",
    "LinearRing",
    "Polygon",
    "MultiGeometry",
    "Track",
    "MultiTrack",
    "Model",
}


def kml_geometry(element, depth=0):
    if depth > 32:
        raise ValueError("KML geometry nesting exceeds the supported depth.")
    kind = local_name(element.tag)
    if kind == "Point":
        coordinates = kml_coordinates(element)
        if len(coordinates) > 1:
            raise ValueError("A Point must contain one coordinate.")
        return Point(coordinates[0]) if coordinates else Point()
    if kind in {"LineString", "LinearRing"}:
        return LineString(kml_coordinates(element))
    if kind == "Polygon":
        outer = []
        holes = []
        for child in element:
            if local_name(child.tag) == "outerBoundaryIs":
                outer = kml_coordinates(child)
            elif local_name(child.tag) == "innerBoundaryIs":
                holes.append(kml_coordinates(child))
        return Polygon(outer, holes) if outer else Polygon()
    if kind == "MultiGeometry":
        children = [child for child in element if local_name(child.tag) in GEOMETRY_TAGS]
        parts = [kml_geometry(child, depth + 1) for child in children]
        if not parts:
            return GeometryCollection()
        if any(part is None for part in parts):
            return None
        if all(isinstance(part, Point) and not part.is_empty for part in parts):
            return MultiPoint(parts)
        if all(isinstance(part, LineString) and not part.is_empty for part in parts):
            return MultiLineString(parts)
        if all(isinstance(part, Polygon) and not part.is_empty for part in parts):
            return MultiPolygon(parts)
        return GeometryCollection(parts)
    return None


def read_kml(path: Path, settings: Settings) -> Dataset:
    try:
        root = SafeXML.parse(path, forbid_dtd=True, forbid_entities=True, forbid_external=True).getroot()
    except (SafeXML.ParseError, DefusedXmlException, RecursionError) as exc:
        raise FileError("INVALID_KML", "The KML is invalid or contains prohibited XML declarations.") from exc
    if root.tag not in {"kml", "{http://www.opengis.net/kml/2.2}kml", "{http://earth.google.com/kml/2.1}kml"}:
        raise FileError("INVALID_KML", "Expected a KML root element and a supported KML namespace.")
    features = []
    total_coordinates = 0
    for placemark in root.iter():
        if local_name(placemark.tag) != "Placemark":
            continue
        if len(features) >= settings.max_features:
            raise FileError("TOO_MANY_FEATURES", "The file exceeds the feature limit.", 413)
        properties = {}
        for child in placemark:
            if local_name(child.tag) in {"name", "description"}:
                properties[local_name(child.tag)] = "".join(child.itertext())
            elif local_name(child.tag) == "ExtendedData":
                for node in child.iter():
                    kind = local_name(node.tag)
                    if kind == "Data" and node.get("name"):
                        value = next((n for n in node if local_name(n.tag) == "value"), None)
                        properties[node.get("name")] = "".join(value.itertext()) if value is not None else ""
                    elif kind == "SimpleData" and node.get("name"):
                        properties[node.get("name")] = "".join(node.itertext())
        nodes = [child for child in placemark if local_name(child.tag) in GEOMETRY_TAGS]
        geometry = None
        kind = local_name(nodes[0].tag) if len(nodes) == 1 else None
        error = None
        # Bound coordinate tokens before constructing Shapely geometries.
        total_coordinates += sum(
            len((node.text or "").split())
            for node in placemark.iter()
            if local_name(node.tag) in {"coordinates", "coord"}
        )
        if total_coordinates > settings.max_coordinates:
            raise FileError("TOO_MANY_COORDINATES", "The file exceeds the coordinate limit.", 413)
        try:
            if len(nodes) > 1:
                raise ValueError("A Placemark with multiple geometries must use MultiGeometry.")
            if nodes:
                parsed = kml_geometry(nodes[0])
                if parsed is not None:
                    kind = parsed.geom_type
                    geometry = json_safe(mapping(parsed)) if not parsed.is_empty else None
        except (ValueError, TypeError, RecursionError, GEOSException) as exc:
            error = str(exc)
        features.append(
            RawFeature(
                placemark.get("id") or str(len(features)),
                geometry,
                kind,
                properties,
                error,
            )
        )
    enforce_budget(features, settings)
    return Dataset(CRS.from_epsg(4326), features, "KML")


def validate_archive(archive, settings):
    members = archive.infolist()
    if len(members) > settings.max_archive_members:
        raise FileError("UNSAFE_ARCHIVE", "The ZIP contains too many members.", 413)
    seen = set()
    total = 0
    for member in members:
        name = member.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or ":" in name
            or "\x00" in name
            or name.casefold() in seen
        ):
            raise FileError("UNSAFE_ARCHIVE", "The ZIP contains unsafe or duplicate paths.")
        seen.add(name.casefold())
        mode = member.external_attr >> 16
        if stat.S_ISLNK(mode) or member.flag_bits & 1:
            raise FileError("UNSAFE_ARCHIVE", "Symlinks and encrypted ZIP members are not accepted.")
        total += member.file_size
        if total > settings.max_archive_bytes or member.file_size / max(member.compress_size, 1) > 200:
            raise FileError("UNSAFE_ARCHIVE", "The ZIP exceeds expansion limits.", 413)
    return members


def read_shapefile(path: Path, settings: Settings) -> Dataset:
    try:
        with zipfile.ZipFile(path) as archive:
            members = validate_archive(archive, settings)
            names = {m.filename.replace("\\", "/").casefold(): m for m in members if not m.is_dir()}
            shapes = [name for name in names if name.endswith(".shp")]
            if len(shapes) != 1:
                raise FileError("INVALID_SHAPEFILE", "The ZIP must contain exactly one Shapefile dataset.")
            stem = shapes[0][:-4]
            required = [".shp", ".shx", ".dbf", ".prj"]
            missing = [ext for ext in required if stem + ext not in names]
            if missing:
                raise FileError("MISSING_SIDECARS", "Missing Shapefile components: " + ", ".join(missing))
            try:
                crs = CRS.from_wkt(archive.read(names[stem + ".prj"]).decode("utf-8-sig"))
            except (CRSError, UnicodeError) as exc:
                raise FileError("INVALID_CRS", "The .prj file does not contain a readable CRS.") from exc
            if not (crs.is_geographic or crs.is_projected):
                raise FileError("UNSUPPORTED_CRS", "Only geographic and projected CRSs are supported.")
            encoding = "utf-8"
            if stem + ".cpg" in names:
                encoding = archive.read(names[stem + ".cpg"]).decode("ascii").strip()
                if encoding.isdigit():
                    encoding = "utf-8" if encoding == "65001" else "cp" + encoding
            # Only selected components are copied, using fixed filenames, never ZIP paths.
            with TemporaryDirectory(prefix="geo-shape-") as temp:
                base = Path(temp) / "dataset"
                for ext in required:
                    with (
                        archive.open(names[stem + ext]) as source,
                        base.with_suffix(ext).open("wb") as target,
                    ):
                        copied = 0
                        while chunk := source.read(1024 * 1024):
                            copied += len(chunk)
                            if copied > settings.max_archive_bytes:
                                raise FileError(
                                    "UNSAFE_ARCHIVE", "A ZIP member exceeds expansion limits.", 413
                                )
                            target.write(chunk)
                features = []
                coordinates = 0
                with shapefile.Reader(str(base), encoding=encoding) as reader:
                    if reader.numRecords > settings.max_features:
                        raise FileError("TOO_MANY_FEATURES", "The file exceeds the feature limit.", 413)
                    if reader.numShapes != reader.numRecords:
                        raise FileError("INVALID_SHAPEFILE", "Shape and attribute record counts differ.")
                    for index, record in enumerate(reader.iterShapeRecords()):
                        coordinates += len(record.shape.points)
                        if coordinates > settings.max_coordinates:
                            raise FileError(
                                "TOO_MANY_COORDINATES", "The file exceeds the coordinate limit.", 413
                            )
                        geometry = None
                        kind = None
                        error = None
                        supported = {
                            shapefile.POINT,
                            shapefile.POINTZ,
                            shapefile.POINTM,
                            shapefile.MULTIPOINT,
                            shapefile.MULTIPOINTZ,
                            shapefile.MULTIPOINTM,
                            shapefile.POLYLINE,
                            shapefile.POLYLINEZ,
                            shapefile.POLYLINEM,
                            shapefile.POLYGON,
                            shapefile.POLYGONZ,
                            shapefile.POLYGONM,
                        }
                        if record.shape.shapeType not in supported | {shapefile.NULL}:
                            kind = record.shape.shapeTypeName
                        elif record.shape.shapeType != shapefile.NULL:
                            try:
                                geometry = record.shape.__geo_interface__
                                kind = geometry["type"]
                                # Validate JSON without converting invalid geometry numbers to null.
                                json.dumps(geometry, allow_nan=False)
                            except (ValueError, TypeError, AttributeError) as exc:
                                error = str(exc)
                                geometry = None
                        features.append(
                            RawFeature(
                                str(index),
                                geometry,
                                kind,
                                json_safe(record.record.as_dict()),
                                error,
                            )
                        )
                enforce_budget(features, settings)
                return Dataset(crs, features, "SHAPEFILE")
    except FileError:
        raise
    except (
        zipfile.BadZipFile,
        OSError,
        ValueError,
        UnicodeError,
        LookupError,
        shapefile.ShapefileException,
        EOFError,
        RuntimeError,
        NotImplementedError,
        struct.error,
        IndexError,
        AssertionError,
    ) as exc:
        raise FileError("INVALID_SHAPEFILE", "The archive or Shapefile data could not be read.") from exc


def safe_filename(filename):
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    if not name or len(name) > 240:
        raise FileError("INVALID_FILENAME", "A filename of at most 240 characters is required.", 400)
    return name
