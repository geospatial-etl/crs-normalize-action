"""CRS interrogation helpers: identity, plausibility and transform availability.

This module holds the parts of the tool that make real geodetic judgements
rather than merely reading metadata:

* :func:`crs_identifier` produces a stable short label for a CRS.
* :func:`plausible_bounds` derives the coordinate envelope a CRS can actually
  produce, by projecting its declared area of use.
* :func:`check_coordinate_plausibility` compares a dataset's bounds against
  that envelope to catch the classic "degrees stored in a projected CRS" and
  "metres stored in EPSG:4326" mistakes.
* :func:`assess_transform` asks PROJ which coordinate operation would be used
  to reach a target CRS, and how accurate it claims to be.
"""

from __future__ import annotations

import logging
import warnings
from functools import lru_cache

from pyproj import CRS, Transformer
from pyproj.transformer import TransformerGroup

from .codes import Code, Severity
from .models import Finding, TransformReport

__all__ = [
    "parse_crs",
    "crs_identifier",
    "crs_display_name",
    "plausible_bounds",
    "check_coordinate_plausibility",
    "assess_transform",
    "is_latitude_first",
]

logger = logging.getLogger(__name__)

#: Padding applied to a CRS's projected area of use before judging a dataset's
#: bounds implausible. Area-of-use polygons are coarse, and legitimate data
#: routinely extends a little past them.
_BOUNDS_TOLERANCE = 0.25

#: Bounds within this envelope look like longitude/latitude degrees.
_DEGREE_ENVELOPE = (-180.0, -90.0, 180.0, 90.0)

#: A projected CRS whose plausible envelope exceeds this magnitude is treated
#: as metre-scale, making degree-magnitude coordinates within it implausible.
_PROJECTED_SCALE_FLOOR = 10_000.0


def parse_crs(value: str) -> CRS:
    """Parse a user-supplied CRS string into a :class:`pyproj.CRS`.

    Args:
        value: An authority code (``"EPSG:3857"``), a PROJ string, WKT, or any
            other form :meth:`pyproj.CRS.from_user_input` accepts.

    Returns:
        The parsed CRS.

    Raises:
        ValueError: If the value cannot be interpreted as a CRS.
    """
    try:
        return CRS.from_user_input(value)
    except Exception as exc:  # pyproj raises a variety of exception types
        raise ValueError(f"{value!r} is not a recognisable CRS: {exc}") from exc


def crs_identifier(crs: CRS | None) -> str | None:
    """Return a compact, stable identifier for ``crs``.

    Prefers an ``AUTHORITY:CODE`` form because that is what users write in
    configuration. Falls back to the CRS name when no authority code can be
    determined, so that unauthored WKT still produces a usable histogram key.

    Args:
        crs: The CRS to label, or ``None``.

    Returns:
        A string such as ``"EPSG:4326"``, or ``None`` when ``crs`` is ``None``.
    """
    if crs is None:
        return None
    authority = crs.to_authority()
    if authority is not None:
        return f"{authority[0]}:{authority[1]}"
    return crs.name or "UNKNOWN"


def crs_display_name(crs: CRS | None) -> str | None:
    """Return the human-readable name of ``crs``, if any."""
    return None if crs is None else crs.name


def is_latitude_first(crs: CRS) -> bool:
    """Return whether ``crs`` declares its northing/latitude axis first.

    Args:
        crs: CRS to inspect.

    Returns:
        ``True`` when the first axis points north or south.
    """
    axes = crs.axis_info
    if not axes:
        return False
    return axes[0].direction.lower() in {"north", "south"}


@lru_cache(maxsize=256)
def plausible_bounds(crs: CRS) -> tuple[float, float, float, float] | None:
    """Return the coordinate envelope ``crs`` can plausibly produce.

    For a geographic CRS this is the degree envelope. For a projected CRS the
    envelope is computed by sampling the CRS's declared area of use on a grid
    and projecting those points, which is far more reliable than assuming a
    fixed range per projection family.

    Args:
        crs: CRS to characterise.

    Returns:
        ``(minx, miny, maxx, maxy)``, or ``None`` when the CRS declares no area
        of use or the projection fails everywhere (in which case no
        plausibility judgement should be made).
    """
    if crs.is_geographic:
        return _DEGREE_ENVELOPE

    area = crs.area_of_use
    if area is None:
        return None

    geodetic = crs.geodetic_crs
    if geodetic is None:
        return None

    try:
        transformer = Transformer.from_crs(geodetic, crs, always_xy=True)
    except Exception:
        logger.debug("Could not build area-of-use transformer for %s", crs.name)
        return None

    steps = 9
    lon_step = (area.east - area.west) / (steps - 1)
    lat_step = (area.north - area.south) / (steps - 1)
    xs: list[float] = []
    ys: list[float] = []
    for i in range(steps):
        for j in range(steps):
            lon = area.west + i * lon_step
            lat = area.south + j * lat_step
            x, y = transformer.transform(lon, lat)
            if _finite(x) and _finite(y):
                xs.append(x)
                ys.append(y)

    if not xs or not ys:
        return None

    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    pad_x = max((maxx - minx) * _BOUNDS_TOLERANCE, 1.0)
    pad_y = max((maxy - miny) * _BOUNDS_TOLERANCE, 1.0)
    return (minx - pad_x, miny - pad_y, maxx + pad_x, maxy + pad_y)


def _finite(value: float) -> bool:
    """Return whether ``value`` is a finite real number."""
    return value == value and value not in (float("inf"), float("-inf"))


def _within(bounds: tuple[float, float, float, float], envelope: tuple[float, float, float, float]) -> bool:
    """Return whether ``bounds`` lies entirely inside ``envelope``."""
    return (
        bounds[0] >= envelope[0]
        and bounds[1] >= envelope[1]
        and bounds[2] <= envelope[2]
        and bounds[3] <= envelope[3]
    )


def check_coordinate_plausibility(
    path,
    crs: CRS,
    bounds: tuple[float, float, float, float] | None,
) -> list[Finding]:
    """Judge whether ``bounds`` could really have been produced by ``crs``.

    Two mistakes dominate real pipelines and both are caught here:

    * Degree-magnitude coordinates carried in a projected CRS, typically the
      result of stamping a projected CRS onto lon/lat data.
    * Coordinates outside the CRS's projected area of use, including the
      ``|x| > 180`` case for a geographic CRS, which means metres were labelled
      as degrees.

    Args:
        path: Dataset path, used only to attach to the finding.
        crs: The CRS the dataset declares.
        bounds: Dataset envelope ``(minx, miny, maxx, maxy)``, or ``None`` when
            the dataset is empty and no judgement is possible.

    Returns:
        Findings describing any implausibility, or an empty list.
    """
    if bounds is None or not all(_finite(v) for v in bounds):
        return []

    if crs.is_geographic:
        if not _within(bounds, _DEGREE_ENVELOPE):
            return [
                Finding(
                    code=Code.IMPLAUSIBLE_COORDINATES,
                    severity=Severity.ERROR,
                    path=path,
                    message=(
                        f"Declared CRS {crs_identifier(crs)} is geographic, so coordinates must lie "
                        f"within longitude -180..180 and latitude -90..90, but the data spans "
                        f"x {bounds[0]:.6g}..{bounds[2]:.6g}, y {bounds[1]:.6g}..{bounds[3]:.6g}. "
                        f"These look like projected coordinates in metres that were labelled as degrees."
                    ),
                    detail={
                        "declared_crs": crs_identifier(crs),
                        "bounds": list(bounds),
                        "expected_envelope": list(_DEGREE_ENVELOPE),
                    },
                )
            ]
        return []

    envelope = plausible_bounds(crs)
    if envelope is None:
        return []

    # A projected CRS whose own envelope runs to tens of kilometres cannot
    # plausibly hold data confined to the ±180/±90 box: that is a degree-shaped
    # extent occupying well under a percent of the CRS's range, right next to
    # the false origin. Local engineering grids with genuinely small
    # coordinates fall below the scale threshold and are left alone.
    envelope_scale = max(abs(v) for v in envelope)
    if _within(bounds, _DEGREE_ENVELOPE) and envelope_scale > _PROJECTED_SCALE_FLOOR:
        return [
            Finding(
                code=Code.IMPLAUSIBLE_COORDINATES,
                severity=Severity.ERROR,
                path=path,
                message=(
                    f"Declared CRS {crs_identifier(crs)} is projected and covers roughly "
                    f"x {envelope[0]:.6g}..{envelope[2]:.6g}, y {envelope[1]:.6g}..{envelope[3]:.6g} "
                    f"in its own units, but the data spans only x {bounds[0]:.6g}..{bounds[2]:.6g}, "
                    f"y {bounds[1]:.6g}..{bounds[3]:.6g}, which is the magnitude of longitude/latitude "
                    f"degrees. The coordinates are almost certainly still geographic."
                ),
                detail={
                    "declared_crs": crs_identifier(crs),
                    "bounds": list(bounds),
                    "expected_envelope": [round(v, 3) for v in envelope],
                    "reason": "degree_magnitude_in_projected_crs",
                },
            )
        ]

    if not _within(bounds, envelope):
        return [
            Finding(
                code=Code.IMPLAUSIBLE_COORDINATES,
                severity=Severity.WARNING,
                path=path,
                message=(
                    f"Data spans x {bounds[0]:.6g}..{bounds[2]:.6g}, y {bounds[1]:.6g}..{bounds[3]:.6g}, "
                    f"which falls outside the area of use of the declared CRS {crs_identifier(crs)} "
                    f"(roughly x {envelope[0]:.6g}..{envelope[2]:.6g}, "
                    f"y {envelope[1]:.6g}..{envelope[3]:.6g}). "
                    f"Either the CRS is wrong or the data extends beyond where this projection is valid."
                ),
                detail={
                    "declared_crs": crs_identifier(crs),
                    "bounds": list(bounds),
                    "expected_envelope": [round(v, 3) for v in envelope],
                    "reason": "outside_area_of_use",
                },
            )
        ]

    return []


def assess_transform(source: CRS, target: CRS) -> TransformReport:
    """Report which coordinate operation PROJ would use from ``source`` to ``target``.

    Uses :class:`pyproj.transformer.TransformerGroup` so that both the selected
    operation and the operations PROJ *could not* use are visible. An empty
    group means the two CRSs are not connected at all; ``best_available``
    being false means the preferred operation needs a datum-shift grid that is
    not installed, and PROJ would silently fall back to a coarser one.

    Args:
        source: CRS to transform from.
        target: CRS to transform to.

    Returns:
        A :class:`TransformReport` describing availability and accuracy.
    """
    source_id = crs_identifier(source) or "unknown"
    target_id = crs_identifier(target) or "unknown"

    if source.equals(target):
        return TransformReport(
            source_crs=source_id,
            target_crs=target_id,
            name="identity (no transformation required)",
            accuracy_m=0.0,
            available=True,
            best_available=True,
        )

    with warnings.catch_warnings():
        # pyproj warns on stderr when the best operation is unavailable; that
        # condition is reported through the returned model instead.
        warnings.simplefilter("ignore")
        try:
            group = TransformerGroup(source, target, always_xy=True)
        except Exception as exc:
            logger.debug("TransformerGroup(%s, %s) failed: %s", source_id, target_id, exc)
            return TransformReport(
                source_crs=source_id,
                target_crs=target_id,
                available=False,
                best_available=False,
            )

    missing_grids = sorted(
        {
            grid.short_name
            for operation in group.unavailable_operations
            for grid in operation.grids
            if not grid.available
        }
    )

    if not group.transformers:
        return TransformReport(
            source_crs=source_id,
            target_crs=target_id,
            available=False,
            best_available=False,
            missing_grids=missing_grids,
        )

    chosen = group.transformers[0]
    accuracy = chosen.accuracy
    return TransformReport(
        source_crs=source_id,
        target_crs=target_id,
        name=chosen.description,
        accuracy_m=None if accuracy is None or accuracy < 0 else float(accuracy),
        available=True,
        best_available=bool(group.best_available),
        missing_grids=missing_grids,
    )
