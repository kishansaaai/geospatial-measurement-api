"""Horizontal metric measurements using feature-local WGS84 projections."""

import math

import numpy as np
from pyproj import CRS, Geod, Transformer
from pyproj.exceptions import ProjError
from shapely import get_coordinates, transform
from shapely.errors import GEOSException
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon, shape
from shapely.validation import explain_validity

from app.readers import RawFeature
from app.schemas import Measurement

WGS84 = CRS.from_epsg(4326)
GEOD = Geod(ellps="WGS84")
MAX_RADIUS_M = 1_000_000
MAX_SEGMENT_M = 5_000
MAX_PROJECTED_COORDINATES = 100_000


def densify(geometry):
    """Approximate shortest geodesic edges before projecting, with a hard vertex cap."""
    remaining = MAX_PROJECTED_COORDINATES

    def sequence(coordinates):
        nonlocal remaining
        points = list(coordinates)
        result = []
        for first, second in zip(points, points[1:]):
            _, _, distance = GEOD.inv(first[0], first[1], second[0], second[1])
            extra = max(0, math.ceil(distance / MAX_SEGMENT_M) - 1)
            remaining -= extra + 1
            if remaining < 1:
                raise ValueError("Geometry exceeds the projected coordinate budget.")
            result.append(first[:2])
            if extra:
                result.extend(GEOD.npts(first[0], first[1], second[0], second[1], extra))
        if points:
            result.append(points[-1][:2])
            remaining -= 1
        return result

    def visit(item):
        if isinstance(item, Polygon):
            return Polygon(sequence(item.exterior.coords), [sequence(ring.coords) for ring in item.interiors])
        if isinstance(item, MultiPolygon):
            return MultiPolygon([visit(part) for part in item.geoms])
        if isinstance(item, LineString):
            return LineString(sequence(item.coords))
        return MultiLineString([visit(part) for part in item.geoms])

    return visit(geometry)


def measure(feature: RawFeature, source_crs: CRS) -> Measurement:
    if feature.error:
        return Measurement(status="INVALID_GEOMETRY", message=feature.error)
    if (
        feature.geometry is None
        and feature.geometry_type is not None
        and feature.geometry_type
        not in {
            "Point",
            "MultiPoint",
            "Polygon",
            "MultiPolygon",
            "LineString",
            "MultiLineString",
            "GeometryCollection",
        }
    ):
        return Measurement(status="UNSUPPORTED", message="This geometry is not supported for measurement.")
    if feature.geometry is None:
        return Measurement(status="EMPTY_GEOMETRY", message="The feature has no geometry.")
    try:
        geometry = shape(feature.geometry)
        if geometry.is_empty:
            return Measurement(status="EMPTY_GEOMETRY", message="The geometry is empty.")
        original_coordinates = get_coordinates(geometry)
        if not np.isfinite(original_coordinates).all():
            return Measurement(status="INVALID_GEOMETRY", message="Geometry contains non-finite coordinates.")
        if source_crs.is_geographic:
            # Validate in the source CRS's angular units, including non-degree CRSs.
            factors = [axis.unit_conversion_factor for axis in source_crs.axis_info[:2]]
            if len(factors) == 2:
                degrees = original_coordinates * np.asarray(factors) * (180 / math.pi)
                if (np.abs(degrees[:, 0]) > 180).any() or (np.abs(degrees[:, 1]) > 90).any():
                    return Measurement(
                        status="INVALID_GEOMETRY", message="Geographic coordinates are outside valid bounds."
                    )
        to_wgs84 = Transformer.from_crs(source_crs, WGS84, always_xy=True)
        geographic = transform(
            geometry, lambda x, y: to_wgs84.transform(x, y, errcheck=True), interleaved=False
        )
        coordinates = get_coordinates(geographic)
        if not np.isfinite(coordinates).all() or (np.abs(coordinates[:, 1]) > 90).any():
            return Measurement(
                status="INVALID_GEOMETRY",
                message="Coordinates cannot be transformed to valid longitude and latitude.",
            )
        if geometry.geom_type in {"Point", "MultiPoint"}:
            return Measurement(status="NO_MEASUREMENT", message="Points do not require a measurement.")
        if geometry.geom_type not in {"Polygon", "MultiPolygon", "LineString", "MultiLineString"}:
            return Measurement(
                status="UNSUPPORTED", message="This geometry type is not supported for measurement."
            )

        # Circular longitude mean correctly centres geometries crossing ±180 degrees.
        radians = np.radians(coordinates[:, 0])
        longitude = math.degrees(math.atan2(np.sin(radians).mean(), np.cos(radians).mean()))
        latitude = float(coordinates[:, 1].mean())
        _, _, radii = GEOD.inv(
            np.full(len(coordinates), longitude),
            np.full(len(coordinates), latitude),
            coordinates[:, 0],
            coordinates[:, 1],
        )
        radius = float(np.max(radii))
        if radius > MAX_RADIUS_M:
            return Measurement(
                status="MEASUREMENT_ERROR",
                message="The feature extends beyond the supported 1,000 km radius of its local projection centre.",
            )
        polygon = geometry.geom_type in {"Polygon", "MultiPolygon"}
        projection = "laea" if polygon else "aeqd"
        definition = (
            f"+proj={projection} +lat_0={latitude:.10f} +lon_0={longitude:.10f} "
            "+datum=WGS84 +units=m +no_defs +type=crs"
        )
        projected_crs = CRS.from_user_input(definition)
        projector = Transformer.from_crs(WGS84, projected_crs, always_xy=True)
        projected = transform(
            densify(geographic),
            lambda x, y: projector.transform(x, y, errcheck=True),
            interleaved=False,
        )
        if not np.isfinite(get_coordinates(projected)).all():
            raise ValueError("Projection produced non-finite coordinates.")
        if not projected.is_valid:
            return Measurement(status="INVALID_GEOMETRY", message=explain_validity(projected))
        warnings = []
        if geometry.has_z:
            warnings.append("Measurements use horizontal coordinates; altitude is ignored.")
        if not polygon and radius > 100_000:
            warnings.append(
                "Local distance projection distortion increases for features beyond 100 km from the centre."
            )
        result = float(projected.area if polygon else projected.length)
        if not math.isfinite(result):
            raise ValueError("Measurement is not finite.")
        return Measurement(
            status="MEASURED",
            area_m2=result if polygon else None,
            length_m=None if polygon else result,
            measurement_crs=definition,
            warnings=warnings,
        )
    except (GEOSException, ProjError, ValueError, TypeError, KeyError, OverflowError) as exc:
        return Measurement(status="MEASUREMENT_ERROR", message=str(exc))
