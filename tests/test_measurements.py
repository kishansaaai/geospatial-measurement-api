import pytest
from pyproj import CRS, Geod, Transformer
from shapely import transform
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, mapping
from shapely.geometry.polygon import orient

from app.measurements import measure
from app.readers import RawFeature

GEOD = Geod(ellps="WGS84")


def result(geometry, crs="EPSG:4326"):
    feature = RawFeature("0", mapping(geometry), geometry.geom_type, {})
    return measure(feature, CRS.from_user_input(crs))


@pytest.mark.parametrize("longitude,latitude", [(77, 12), (-74, 40), (179.995, 10), (20, 85), (20, -85)])
def test_length_matches_independent_geodesic(longitude, latitude):
    end_longitude = (longitude + 0.01 + 180) % 360 - 180
    geometry = LineString([(longitude, latitude), (end_longitude, latitude)])
    measured = result(geometry)
    _, _, expected = GEOD.inv(longitude, latitude, end_longitude, latitude)
    assert measured.status == "MEASURED"
    assert measured.length_m == pytest.approx(expected, rel=1e-5)
    assert "+proj=aeqd" in measured.measurement_crs


def test_polygon_holes_against_independent_ellipsoidal_area():
    geometry = Polygon(
        [(77, 12), (77.1, 12), (77.1, 12.1), (77, 12.1)],
        [[(77.02, 12.02), (77.02, 12.04), (77.04, 12.04), (77.04, 12.02)]],
    )
    expected, _ = GEOD.geometry_area_perimeter(orient(geometry, sign=1))
    measured = result(geometry)
    assert measured.status == "MEASURED"
    assert measured.area_m2 == pytest.approx(abs(expected), rel=1e-5)
    assert "+proj=laea" in measured.measurement_crs


def test_antimeridian_polygon():
    geometry = Polygon([(179.9, 10), (-179.9, 10), (-179.9, 10.1), (179.9, 10.1)])
    expected, _ = GEOD.geometry_area_perimeter(geometry)
    measured = result(geometry)
    assert measured.status == "MEASURED"
    assert measured.area_m2 == pytest.approx(abs(expected), rel=1e-5)


def test_projected_input_does_not_use_web_mercator_area():
    geographic = Polygon([(77, 60), (77.01, 60), (77.01, 60.01), (77, 60.01)])
    projector = Transformer.from_crs(4326, 3857, always_xy=True)
    projected = transform(geographic, projector.transform, interleaved=False)
    measured = result(projected, "EPSG:3857")
    expected, _ = GEOD.geometry_area_perimeter(geographic)
    assert measured.area_m2 == pytest.approx(abs(expected), rel=1e-5)
    assert measured.area_m2 < projected.area / 3


def test_projected_feet_input_returns_square_metres():
    geometry = Polygon(
        [(1_000_000, 200_000), (1_001_000, 200_000), (1_001_000, 201_000), (1_000_000, 201_000)]
    )
    measured = result(geometry, "EPSG:2263")
    assert measured.status == "MEASURED"
    assert measured.area_m2 == pytest.approx(92_903.41, rel=0.002)


def test_multipart_area_and_length():
    polygon = Polygon([(77, 12), (77.01, 12), (77.01, 12.01), (77, 12.01)])
    other = Polygon([(77.02, 12), (77.03, 12), (77.03, 12.01), (77.02, 12.01)])
    measured = result(MultiPolygon([polygon, other]))
    assert measured.area_m2 == pytest.approx(result(polygon).area_m2 + result(other).area_m2, rel=1e-5)
    line = LineString([(77, 12), (77.01, 12)])
    assert result(MultiLineString([line, line])).length_m == pytest.approx(
        2 * result(line).length_m, rel=1e-5
    )


def test_invalid_self_intersecting_polygon():
    polygon = Polygon([(77, 12), (77.01, 12.01), (77, 12.01), (77.01, 12), (77, 12)])
    assert result(polygon).status == "INVALID_GEOMETRY"


def test_point_and_invalid_geographic_point():
    assert result(Point(77, 12)).status == "NO_MEASUREMENT"
    assert result(Point(77, 100)).status == "INVALID_GEOMETRY"


def test_global_geometry_and_altitude():
    assert result(LineString([(0, 0), (100, 0)])).status == "MEASUREMENT_ERROR"
    measured = result(LineString([(77, 12, 100), (77.01, 12, 300)]))
    assert measured.status == "MEASURED"
    assert measured.warnings and "altitude" in measured.warnings[0]
