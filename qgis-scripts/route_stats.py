"""
Route Stats - QGIS Processing script
====================================

For every line in a layer, adds these fields (creating them if needed):

    dist_mi   length in miles (measured on the WGS84 ellipsoid)
    gain_ft   total elevation gain, feet
    loss_ft   total elevation loss, feet
    max_ft    highest point, feet
    min_ft    lowest point, feet
    profile   path to an elevation-profile PNG (if profiles are on)

Elevation comes from AWS Terrain Tiles - the same data behind QGIS's built-in
"Mapzen Global Terrain" layer (USGS 3DEP ~10 m in the US) - unless you pick a
DEM raster of your own. Downloaded tiles are cached on disk, so re-runs are
fast and work offline for areas you've already processed.

Install: Processing Toolbox -> Python icon -> "Add Script to Toolbox..."
It then appears under Scripts > Hiking > Route stats.
"""

import math
import os
import re

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QImage
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsField,
    QgsGeometry,
    QgsNetworkAccessManager,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterVectorLayer,
)

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TILE_ZOOM = 14          # ~8 m pixels at California latitudes; source data is ~10 m
M_PER_MI = 1609.344
FT_PER_M = 3.28084

# --------------------------------------------------------------------------
# Compatibility between QGIS 3.x versions and QGIS 4 (old vs new enum names)
# --------------------------------------------------------------------------
try:
    LINE_LAYERS = Qgis.ProcessingSourceType.VectorLine
except AttributeError:
    LINE_LAYERS = QgsProcessing.TypeVectorLine
try:
    NUM_DOUBLE = Qgis.ProcessingNumberParameterType.Double
except AttributeError:
    NUM_DOUBLE = QgsProcessingParameterNumber.Double
try:
    FOLDER = Qgis.ProcessingFileParameterBehavior.Folder
except AttributeError:
    FOLDER = QgsProcessingParameterFile.Folder
try:
    NO_THREADING = Qgis.ProcessingAlgorithmFlag.NoThreading
except AttributeError:
    NO_THREADING = QgsProcessingAlgorithm.FlagNoThreading


def make_field(name, kind, length=0, prec=0):
    """Create a QgsField that works on old and new QGIS versions."""
    try:
        from qgis.PyQt.QtCore import QMetaType
        types = {"double": QMetaType.Type.Double, "int": QMetaType.Type.Int,
                 "str": QMetaType.Type.QString}
        return QgsField(name, types[kind], len=length, prec=prec)
    except (TypeError, AttributeError):
        from qgis.PyQt.QtCore import QVariant
        types = {"double": QVariant.Double, "int": QVariant.Int,
                 "str": QVariant.String}
        return QgsField(name, types[kind], len=length, prec=prec)


# --------------------------------------------------------------------------
# Pure helpers (no QGIS needed)
# --------------------------------------------------------------------------
def lonlat_to_global_px(lon, lat, zoom):
    """Web Mercator global pixel coordinates (256 px tiles) for a lon/lat."""
    lat = max(min(lat, 85.05112878), -85.05112878)
    size = 256 * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * size
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * size
    return x, y


def decode_terrarium(r, g, b):
    """Terrarium PNG pixel -> elevation in metres."""
    return r * 256.0 + g + b / 256.0 - 32768.0


def gain_loss(elevs, threshold):
    """
    Total climb and descent, ignoring wiggles smaller than `threshold` metres.
    A change only counts once it moves at least `threshold` away from the
    last counted point, which filters out DEM noise. Gain - loss always
    equals end elevation - start elevation.
    """
    if len(elevs) < 2:
        return 0.0, 0.0
    gain = loss = 0.0
    ref = elevs[0]
    for e in elevs[1:]:
        d = e - ref
        if d >= threshold and d > 0:
            gain += d
            ref = e
        elif d <= -threshold and d < 0:
            loss -= d
            ref = e
    d = elevs[-1] - ref           # count the last partial step
    if d > 0:
        gain += d
    else:
        loss -= d
    return gain, loss


def clean_text(value):
    """Attribute value -> plain string ('' for NULL)."""
    if value is None:
        return ""
    try:
        if hasattr(value, "isNull") and value.isNull():
            return ""
    except Exception:
        pass
    s = str(value).strip()
    return "" if s == "NULL" else s


# --------------------------------------------------------------------------
# Elevation sources
# --------------------------------------------------------------------------
class TerrariumSampler:
    """Reads elevation from AWS Terrain Tiles with an on-disk tile cache."""

    def __init__(self, zoom, cache_dir, feedback):
        self.z = zoom
        self.size = 256 * (2 ** zoom)
        self.cache_dir = cache_dir
        self.feedback = feedback
        self.tiles = {}
        os.makedirs(cache_dir, exist_ok=True)

    def _tile(self, tx, ty):
        key = (tx, ty)
        if key in self.tiles:
            return self.tiles[key]
        path = os.path.join(self.cache_dir, "{}_{}_{}.png".format(self.z, tx, ty))
        img = QImage()
        if os.path.exists(path):
            img.load(path)
        if img.isNull():
            url = TILE_URL.format(z=self.z, x=tx, y=ty)
            reply = QgsNetworkAccessManager.instance().blockingGet(
                QNetworkRequest(QUrl(url)))
            data = reply.content()
            img = QImage.fromData(data, "PNG")
            if img.isNull():
                raise QgsProcessingException(
                    "Couldn't download elevation tile:\n{}\n"
                    "Check your internet connection.".format(url))
            with open(path, "wb") as f:
                f.write(data.data())
        self.tiles[key] = img
        return img

    def _px(self, gx, gy):
        gx %= self.size
        gy = min(max(gy, 0), self.size - 1)
        c = self._tile(gx // 256, gy // 256).pixelColor(gx % 256, gy % 256)
        return decode_terrarium(c.red(), c.green(), c.blue())

    def elevation(self, lon, lat):
        """Bilinear-interpolated elevation (m) at a WGS84 lon/lat."""
        x, y = lonlat_to_global_px(lon, lat, self.z)
        fx, fy = x - 0.5, y - 0.5          # pixel values sit at pixel centres
        x0, y0 = int(math.floor(fx)), int(math.floor(fy))
        tx, ty = fx - x0, fy - y0
        return (self._px(x0, y0) * (1 - tx) * (1 - ty)
                + self._px(x0 + 1, y0) * tx * (1 - ty)
                + self._px(x0, y0 + 1) * (1 - tx) * ty
                + self._px(x0 + 1, y0 + 1) * tx * ty)


class RasterSampler:
    """Reads elevation from a DEM raster layer (band 1)."""

    def __init__(self, layer, wgs84, transform_context):
        self.provider = layer.dataProvider()
        self.xform = QgsCoordinateTransform(wgs84, layer.crs(), transform_context)

    def elevation(self, lon, lat):
        pt = self.xform.transform(QgsPointXY(lon, lat))
        value, ok = self.provider.sample(pt, 1)
        if not ok or value is None or math.isnan(value):
            return None
        return value


# --------------------------------------------------------------------------
# Geometry sampling
# --------------------------------------------------------------------------
def sample_line(geom, da, spacing):
    """
    Walk a WGS84 line and return [(distance_m, lon, lat), ...] every
    ~`spacing` metres. Multipart lines are chained without counting gaps.
    """
    if geom.isMultipart():
        parts = geom.asMultiPolyline()
    else:
        parts = [geom.asPolyline()]
    out = []
    cum = 0.0
    for part in parts:
        if len(part) < 2:
            continue
        out.append((cum, part[0].x(), part[0].y()))
        for a, b in zip(part[:-1], part[1:]):
            seg = da.measureLine(a, b)
            steps = max(1, int(math.ceil(seg / spacing)))
            for i in range(1, steps + 1):
                t = i / steps
                out.append((cum + t * seg,
                            a.x() + t * (b.x() - a.x()),
                            a.y() + t * (b.y() - a.y())))
            cum += seg
    return out


def save_profile_png(path, dist_mi, elev_ft, title, subtitle):
    from matplotlib.figure import Figure   # no pyplot = no global state

    fig = Figure(figsize=(8, 3), dpi=120)
    ax = fig.subplots()
    lo, hi = min(elev_ft), max(elev_ft)
    pad = max(25.0, (hi - lo) * 0.12)
    ax.plot(dist_mi, elev_ft, color="#b5452f", linewidth=1.6)
    ax.fill_between(dist_mi, elev_ft, lo - pad, color="#b5452f", alpha=0.15)
    ax.set_xlim(0, max(dist_mi[-1], 0.01))
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlabel("Distance (mi)")
    ax.set_ylabel("Elevation (ft)")
    ax.set_title(title, loc="left", fontweight="bold")
    ax.text(1.0, 1.02, subtitle, transform=ax.transAxes,
            ha="right", va="bottom", fontsize=9, color="#555555")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path)


# --------------------------------------------------------------------------
# The algorithm
# --------------------------------------------------------------------------
class RouteStats(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    DEM = "DEM"
    SPACING = "SPACING"
    THRESHOLD = "THRESHOLD"
    PROFILES = "PROFILES"
    PROFILE_DIR = "PROFILE_DIR"

    def name(self):
        return "route_stats"

    def displayName(self):
        return "Route stats (distance, gain, profile)"

    def group(self):
        return "Hiking"

    def groupId(self):
        return "hiking"

    def shortHelpString(self):
        return (
            "Adds distance (mi), elevation gain/loss (ft), highest/lowest "
            "point (ft) and an elevation-profile image to every line in the "
            "layer. Results are written straight into the layer's fields, so "
            "labels and map tips can use them.\n\n"
            "If the layer is being edited, your edits are saved first and "
            "editing is turned back on afterwards.\n\n"
            "Elevation: leave 'Elevation raster' blank to use AWS Terrain "
            "Tiles (same as the Mapzen Global Terrain layer), or pick a "
            "downloaded DEM file for higher accuracy.\n\n"
            "Gain threshold: climbs or drops smaller than this are treated "
            "as DEM noise. Raise it if gain looks too high; lower it for "
            "very gentle terrain."
        )

    def flags(self):
        # Runs on the main thread because it edits the layer in place.
        return super().flags() | NO_THREADING

    def createInstance(self):
        return RouteStats()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT, "Route layer (lines)", [LINE_LAYERS]))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.DEM, "Elevation raster (optional; blank = AWS Terrain Tiles)",
            optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.SPACING, "Sample every (m)", type=NUM_DOUBLE,
            defaultValue=10.0, minValue=1.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.THRESHOLD, "Gain threshold (m)", type=NUM_DOUBLE,
            defaultValue=3.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.PROFILES, "Save elevation profile images", defaultValue=True))
        self.addParameter(QgsProcessingParameterFile(
            self.PROFILE_DIR,
            "Profile image folder (optional; blank = 'profiles' next to the layer)",
            behavior=FOLDER, optional=True))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        dem = self.parameterAsRasterLayer(parameters, self.DEM, context)
        spacing = self.parameterAsDouble(parameters, self.SPACING, context)
        threshold = self.parameterAsDouble(parameters, self.THRESHOLD, context)
        make_png = self.parameterAsBoolean(parameters, self.PROFILES, context)
        png_dir = self.parameterAsFile(parameters, self.PROFILE_DIR, context)

        if layer is None:
            raise QgsProcessingException("Pick a line layer.")

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        tctx = context.transformContext()

        # Elevation source. The Mapzen XYZ layer can't be sampled directly,
        # so read its tiles ourselves instead.
        if dem is not None and dem.providerType() == "wms" \
                and "terrarium" in dem.source().lower():
            dem = None
        if dem is None:
            cache = os.path.join(QgsApplication.qgisSettingsDirPath(),
                                 "cache", "terrarium_tiles")
            sampler = TerrariumSampler(TILE_ZOOM, cache, feedback)
            feedback.pushInfo("Elevation: AWS Terrain Tiles (cached in {})".format(cache))
        else:
            sampler = RasterSampler(dem, wgs84, tctx)
            feedback.pushInfo("Elevation: {}".format(dem.name()))

        # Profile images
        if make_png:
            try:
                import matplotlib  # noqa: F401
            except ImportError:
                feedback.reportError("matplotlib isn't available, so profile "
                                     "images will be skipped.", False)
                make_png = False
        if make_png:
            if not png_dir:
                src = layer.source().split("|")[0]
                base = os.path.dirname(src) if os.path.isfile(src) \
                    else QgsApplication.qgisSettingsDirPath()
                png_dir = os.path.join(base, "profiles")
            os.makedirs(png_dir, exist_ok=True)

        # Save pending edits so we can write to the file directly.
        was_editing = layer.isEditable()
        if was_editing and not layer.commitChanges():
            raise QgsProcessingException(
                "Couldn't save your edits on '{}':\n{}".format(
                    layer.name(), "\n".join(layer.commitErrors())))

        # Make sure the output fields exist.
        wanted = [("dist_mi", "double", 10, 2), ("gain_ft", "int", 10, 0),
                  ("loss_ft", "int", 10, 0), ("max_ft", "int", 10, 0),
                  ("min_ft", "int", 10, 0)]
        if make_png:
            wanted.append(("profile", "str", 254, 0))
        pr = layer.dataProvider()
        have = {f.name().lower() for f in layer.fields()}
        new = [make_field(*w) for w in wanted if w[0] not in have]
        if new:
            if not pr.addAttributes(new):
                raise QgsProcessingException("Couldn't add fields to the layer.")
            layer.updateFields()
        idx = {w[0]: layer.fields().lookupField(w[0]) for w in wanted}
        name_idx = layer.fields().lookupField("name")

        to_wgs = QgsCoordinateTransform(layer.crs(), wgs84, tctx)
        da = QgsDistanceArea()
        da.setSourceCrs(wgs84, tctx)
        da.setEllipsoid("EPSG:7030")      # WGS84 ellipsoid -> metres

        features = list(layer.getFeatures())
        changes = {}
        for n, feat in enumerate(features):
            if feedback.isCanceled():
                break
            feedback.setProgress(100.0 * n / max(1, len(features)))

            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            g = QgsGeometry(geom)
            g.transform(to_wgs)

            samples = sample_line(g, da, spacing)
            if len(samples) < 2:
                continue
            dists, elevs = [], []
            for d, lon, lat in samples:
                e = sampler.elevation(lon, lat)
                if e is not None:
                    dists.append(d)
                    elevs.append(e)

            name = clean_text(feat[name_idx]) if name_idx >= 0 else ""
            label = name or "Route {}".format(feat.id())
            if len(elevs) < 2:
                feedback.pushWarning("{}: no elevation data here, skipped.".format(label))
                continue

            length_m = samples[-1][0]
            gain_m, loss_m = gain_loss(elevs, threshold)
            vals = {
                idx["dist_mi"]: round(length_m / M_PER_MI, 2),
                idx["gain_ft"]: int(round(gain_m * FT_PER_M)),
                idx["loss_ft"]: int(round(loss_m * FT_PER_M)),
                idx["max_ft"]: int(round(max(elevs) * FT_PER_M)),
                idx["min_ft"]: int(round(min(elevs) * FT_PER_M)),
            }
            summary = "{:.2f} mi  +{:,} ft / -{:,} ft".format(
                vals[idx["dist_mi"]], vals[idx["gain_ft"]], vals[idx["loss_ft"]])

            if make_png:
                safe = re.sub(r"[^\w\-]+", "_", label).strip("_")[:40] or "route"
                path = os.path.join(png_dir, "{}_{}.png".format(safe, feat.id()))
                save_profile_png(path,
                                 [d / M_PER_MI for d in dists],
                                 [e * FT_PER_M for e in elevs],
                                 label, summary)
                vals[idx["profile"]] = path.replace("\\", "/")

            changes[feat.id()] = vals
            feedback.pushInfo("{}: {}".format(label, summary))

        if changes and not pr.changeAttributeValues(changes):
            raise QgsProcessingException("Couldn't write results to the layer.")

        if was_editing:
            layer.startEditing()
        layer.triggerRepaint()
        feedback.pushInfo("Updated {} route(s).".format(len(changes)))
        if make_png:
            feedback.pushInfo("Profiles saved in {}".format(png_dir))
        return {}
