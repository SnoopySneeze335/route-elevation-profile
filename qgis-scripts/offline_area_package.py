"""
Offline Area Package - QGIS Processing script
=============================================

Builds an offline map package around a trip:

  1. Draws a box around everything in the trip layers you pick (points
     and/or lines) and pads it on every side (default 2 miles).
  2. Saves each basemap you tick as an MBTiles file covering that box.
  3. Builds an elevation model (DEM) for the box from AWS Terrain Tiles,
     the same data behind QGIS's "Mapzen Global Terrain" layer.
  4. Makes contour lines in feet, with thicker labelled index contours.
  5. Adds everything to the project in one group.

Everything is saved in a "<trip>_offline" folder, so it can be reopened
later with no internet. The DEM also works as the "Elevation raster" in
the Route stats script and in the Elevation Profile panel.

Install: Processing Toolbox -> Python icon -> "Add Script to Toolbox..."
It then appears under Scripts > Hiking > Offline area package.
"""

import math
import os
import re
import sqlite3

import numpy as np
from osgeo import gdal, ogr, osr

from qgis.PyQt.QtCore import QRunnable, QThreadPool, QUrl
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDataSourceUri,
    QgsFillSymbol,
    QgsLayerTree,
    QgsLineSymbol,
    QgsNetworkAccessManager,
    QgsPalLayerSettings,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingMultiStepFeedback,
    QgsProcessingOutputFolder,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFile,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsRuleBasedRenderer,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
TERRAIN_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
DEM_ZOOM = 14              # ~8 m pixels in California; source data is ~10 m
MAX_DEM_TILES = 400        # drop to a coarser zoom for very large areas
MAX_BASEMAP_TILES = 20000  # per basemap; stops runaway downloads
DOWNLOAD_THREADS = 8      # tiles fetched at once (raise for faster, if the server allows)
OSM_THREADS = 2           # OpenStreetMap's tile policy asks for few connections
CHUNK = 64                # tiles per batch (cancel + progress granularity)
INDEX_EVERY = 5            # every 5th contour is a thick, labelled index line
CONTOUR_SCALE = 24000      # all contours + labels show at 1:24,000 or closer
INDEX_SCALE = 100000       # index contours alone show out to 1:100,000
WEB_MERC_MAX = 20037508.342789244
EARTH_R = 6378137.0
M_PER_MI = 1609.344
FT_PER_M = 3.28084

# --------------------------------------------------------------------------
# Compatibility between QGIS 3.x versions and QGIS 4 (old vs new enum names)
# --------------------------------------------------------------------------
def _enum(new, old):
    try:
        return new()
    except AttributeError:
        return old()


ANY_VECTOR = _enum(lambda: Qgis.ProcessingSourceType.VectorAnyGeometry,
                   lambda: QgsProcessing.TypeVectorAnyGeometry)
RASTER = _enum(lambda: Qgis.ProcessingSourceType.Raster,
               lambda: QgsProcessing.TypeRaster)
NUM_DOUBLE = _enum(lambda: Qgis.ProcessingNumberParameterType.Double,
                   lambda: QgsProcessingParameterNumber.Double)
NUM_INT = _enum(lambda: Qgis.ProcessingNumberParameterType.Integer,
                lambda: QgsProcessingParameterNumber.Integer)
FOLDER_BEHAVIOR = _enum(lambda: Qgis.ProcessingFileParameterBehavior.Folder,
                        lambda: QgsProcessingParameterFile.Folder)
CURVED_LABELS = _enum(lambda: Qgis.LabelPlacement.Curved,
                      lambda: QgsPalLayerSettings.Curved)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def safe_name(text):
    return re.sub(r"[^\w\-]+", "_", text).strip("_")[:60] or "layer"


def merc_y_to_lat(y):
    return math.degrees(2 * math.atan(math.exp(y / EARTH_R)) - math.pi / 2)


def tile_range(box, z):
    """Inclusive tile ranges (tx0, tx1, ty0, ty1) covering an EPSG:3857 box."""
    xmin, ymin, xmax, ymax = box
    n = 2 ** z
    span = 2 * WEB_MERC_MAX / n

    def clamp(v):
        return max(0, min(n - 1, v))

    return (clamp(int(math.floor((xmin + WEB_MERC_MAX) / span))),
            clamp(int(math.floor((xmax + WEB_MERC_MAX) / span))),
            clamp(int(math.floor((WEB_MERC_MAX - ymax) / span))),
            clamp(int(math.floor((WEB_MERC_MAX - ymin) / span))))


def tile_count(box, zmin, zmax):
    total = 0
    for z in range(zmin, zmax + 1):
        tx0, tx1, ty0, ty1 = tile_range(box, z)
        total += (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    return total


def decode_terrarium(rgb):
    """(3, h, w) uint8 Terrarium pixels -> elevation in metres."""
    r, g, b = (rgb[i].astype(np.float64) for i in range(3))
    return r * 256.0 + g + b / 256.0 - 32768.0


def smooth3(a):
    """Light 3x3 average, so contours aren't jagged."""
    p = np.pad(a, 1, mode="edge")
    h, w = a.shape
    return sum(p[i:i + h, j:j + w] for i in range(3) for j in range(3)) / 9.0


def merc_to_lonlat(x, y):
    return math.degrees(x / EARTH_R), merc_y_to_lat(y)


def quadkey(x, y, z):
    digits = []
    for i in range(z, 0, -1):
        mask = 1 << (i - 1)
        digits.append(str((1 if x & mask else 0) + (2 if y & mask else 0)))
    return "".join(digits)


def tile_url(template, z, x, y):
    url = (template.replace("{z}", str(z)).replace("{x}", str(x))
           .replace("{-y}", str(2 ** z - 1 - y)).replace("{y}", str(y)))
    return url.replace("{q}", quadkey(x, y, z)) if "{q}" in url else url


def image_format(data):
    """'png' / 'jpg' / 'webp' for image bytes, None for anything else."""
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# --------------------------------------------------------------------------
# Parallel downloads (Qt worker threads, so QGIS's network timeouts work)
# --------------------------------------------------------------------------
class _Job(QRunnable):
    def __init__(self, func, item, results, index):
        super().__init__()
        self.setAutoDelete(False)
        self.func, self.item, self.results, self.index = func, item, results, index

    def run(self):
        try:
            self.results[self.index] = self.func(self.item)
        except Exception:
            self.results[self.index] = None


class Downloader:
    """Runs func(item) for many items, `threads` at a time."""

    def __init__(self, threads):
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(max(1, threads))

    def map(self, func, items):
        results = [None] * len(items)
        jobs = [_Job(func, item, results, i) for i, item in enumerate(items)]
        for job in jobs:
            self.pool.start(job)
        self.pool.waitForDone()
        return results


# --------------------------------------------------------------------------
# Network + basemap tiles -> MBTiles
# --------------------------------------------------------------------------
def fetch_url(url, referer=""):
    """Download bytes with QGIS's network settings (proxy, certificates)."""
    req = QNetworkRequest(QUrl(url))
    if referer:
        req.setRawHeader(b"Referer", referer.encode())
    return QgsNetworkAccessManager.instance().blockingGet(req).content().data()


def xyz_info(layer):
    """(url template, zmin, zmax, referer) for an XYZ tile layer, else None."""
    if layer.providerType() != "wms":
        return None
    uri = QgsDataSourceUri()
    uri.setEncodedUri(layer.source())
    url = uri.param("url")
    if uri.param("type") != "xyz" or not url:
        return None

    def as_int(name, default):
        try:
            return int(uri.param(name))
        except (TypeError, ValueError):
            return default

    referer = uri.param("http-header:referer") or uri.param("referer") or ""
    return url, as_int("zmin", 0), as_int("zmax", 19), referer


def fetch_tile(template, referer, z, x, y):
    """One basemap tile's bytes, or None (one retry for flaky connections)."""
    url = tile_url(template, z, x, y)
    for _ in range(2):
        data = fetch_url(url, referer)
        if image_format(data):
            return data
    return None


def download_mbtiles(info, box, zmin, zmax, out_path, title, feedback, threads):
    """
    Copy a basemap's tiles for `box` into an MBTiles file, `threads` at a
    time. Tiles already in the file (from an earlier or canceled run) are
    kept and skipped, so re-running resumes where it stopped.
    Returns (tiles stored, tiles the server didn't have).
    """
    url, _, _, referer = info
    con = sqlite3.connect(out_path)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT);"
        "CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, "
        "tile_column INTEGER, tile_row INTEGER, tile_data BLOB);"
        "CREATE UNIQUE INDEX IF NOT EXISTS tile_index ON tiles "
        "(zoom_level, tile_column, tile_row);")
    have = set(con.execute("SELECT zoom_level, tile_column, tile_row FROM tiles"))
    todo = []
    for z in range(zmin, zmax + 1):
        tx0, tx1, ty0, ty1 = tile_range(box, z)
        for x in range(tx0, tx1 + 1):
            for y in range(ty0, ty1 + 1):
                if (z, x, 2 ** z - 1 - y) not in have:
                    todo.append((z, x, y))
    total = max(1, tile_count(box, zmin, zmax))
    already = total - len(todo)
    if already > 0:
        feedback.pushInfo("{}: {:,} tiles already saved earlier; downloading "
                          "the other {:,}.".format(title, already, len(todo)))

    fmt, missing = None, 0
    try:
        pool = Downloader(threads)
        for i in range(0, len(todo), CHUNK):
            if feedback.isCanceled():
                raise QgsProcessingException(
                    "Canceled. Tiles downloaded so far are kept; run it "
                    "again to pick up where it stopped.")
            chunk = todo[i:i + CHUNK]
            results = pool.map(lambda t: fetch_tile(url, referer, *t), chunk)
            for (z, x, y), data in zip(chunk, results):
                if data:
                    fmt = fmt or image_format(data)
                    con.execute(
                        "INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)",
                        (z, x, 2 ** z - 1 - y, sqlite3.Binary(data)))
                else:
                    missing += 1
            con.commit()
            feedback.setProgress(100.0 * (already + i + len(chunk)) / total)

        if fmt is None:
            row = con.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
            fmt = image_format(row[0]) if row else None
        w, s_ = merc_to_lonlat(box[0], box[1])
        e, n = merc_to_lonlat(box[2], box[3])
        con.execute("DELETE FROM metadata")
        con.executemany("INSERT INTO metadata VALUES (?,?)", [
            ("name", title), ("type", "baselayer"), ("version", "1.1"),
            ("description", "Offline copy made in QGIS"),
            ("format", fmt or "png"),
            ("bounds", "{:.6f},{:.6f},{:.6f},{:.6f}".format(w, s_, e, n)),
            ("center", "{:.6f},{:.6f},{}".format((w + e) / 2, (s_ + n) / 2, zmin)),
            ("minzoom", str(zmin)), ("maxzoom", str(zmax))])
        con.commit()
        stored = con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    finally:
        con.close()
    if stored == 0 and os.path.exists(out_path):
        os.remove(out_path)
    return stored, missing


# --------------------------------------------------------------------------
# Terrain tiles -> DEM -> contours
# --------------------------------------------------------------------------
def download_bytes(url):
    return fetch_url(url)


def cache_terrain_tile(z, x, y, cache_dir):
    """Download a terrain tile into the disk cache if it isn't there yet."""
    path = os.path.join(cache_dir, "{}_{}_{}.png".format(z, x, y))
    if not os.path.exists(path):
        data = download_bytes(TERRAIN_URL.format(z=z, x=x, y=y))
        if image_format(data):
            part = "{}.{}.part".format(path, os.getpid())
            with open(part, "wb") as f:
                f.write(data)
            os.replace(part, path)
    return path


def terrarium_tile(z, x, y, cache_dir):
    """One terrain tile as a (256, 256) array of metres, using a disk cache."""
    path = cache_terrain_tile(z, x, y, cache_dir)
    ds = gdal.Open(path) if os.path.exists(path) else None
    if ds is None or ds.RasterCount < 3:
        ds = None
        if os.path.exists(path):
            os.remove(path)
        raise QgsProcessingException(
            "Couldn't get elevation tile {}/{}/{}. Check your internet "
            "connection and try again.".format(z, x, y))
    return decode_terrarium(ds.ReadAsArray()[:3])


def build_dem(box, z, cache_dir, out_path, feedback):
    """Mosaic terrain tiles over `box`, crop, and save a GeoTIFF (metres)."""
    os.makedirs(cache_dir, exist_ok=True)
    tx0, tx1, ty0, ty1 = tile_range(box, z)
    nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    Downloader(DOWNLOAD_THREADS).map(
        lambda t: cache_terrain_tile(z, t[0], t[1], cache_dir),
        [(x, y) for y in range(ty0, ty1 + 1) for x in range(tx0, tx1 + 1)])
    mosaic = np.zeros((ny * 256, nx * 256), dtype=np.float32)
    done = 0
    for j, ty in enumerate(range(ty0, ty1 + 1)):
        for i, tx in enumerate(range(tx0, tx1 + 1)):
            if feedback.isCanceled():
                raise QgsProcessingException("Canceled.")
            mosaic[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = \
                terrarium_tile(z, tx, ty, cache_dir)
            done += 1
            feedback.setProgress(100.0 * done / (nx * ny))

    span = 2 * WEB_MERC_MAX / 2 ** z
    px = span / 256.0
    left = -WEB_MERC_MAX + tx0 * span
    top = WEB_MERC_MAX - ty0 * span
    c0 = max(0, int(math.floor((box[0] - left) / px)))
    c1 = min(nx * 256, int(math.ceil((box[2] - left) / px)))
    r0 = max(0, int(math.floor((top - box[3]) / px)))
    r1 = min(ny * 256, int(math.ceil((top - box[1]) / px)))
    dem = mosaic[r0:r1, c0:c1]
    gt = (left + c0 * px, px, 0.0, top - r0 * px, 0.0, -px)

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    if os.path.exists(out_path):
        gdal.GetDriverByName("GTiff").Delete(out_path)
    ds = gdal.GetDriverByName("GTiff").Create(
        out_path, dem.shape[1], dem.shape[0], 1, gdal.GDT_Float32,
        ["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES"])
    if ds is None:
        raise QgsProcessingException("Couldn't write {}".format(out_path))
    ds.SetGeoTransform(gt)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(dem)
    ds.FlushCache()
    ds = None
    return dem, gt


def write_area_and_contours(gpkg_path, box, dem, gt, interval_ft):
    """GeoPackage with an 'area' box layer and, optionally, 'contours'."""
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(gpkg_path):
        drv.DeleteDataSource(gpkg_path)
    ds = drv.CreateDataSource(gpkg_path)
    if ds is None:
        raise QgsProcessingException("Couldn't create {}".format(gpkg_path))

    area = ds.CreateLayer("area", srs, ogr.wkbPolygon)
    xmin, ymin, xmax, ymax = box
    feat = ogr.Feature(area.GetLayerDefn())
    feat.SetGeometry(ogr.CreateGeometryFromWkt(
        "POLYGON(({0} {1},{2} {1},{2} {3},{0} {3},{0} {1}))".format(
            xmin, ymin, xmax, ymax)))
    area.CreateFeature(feat)
    feat = None

    count = 0
    if interval_ft:
        feet = smooth3(dem.astype(np.float64)) * FT_PER_M
        mem = gdal.GetDriverByName("MEM").Create(
            "", dem.shape[1], dem.shape[0], 1, gdal.GDT_Float32)
        mem.SetGeoTransform(gt)
        mem.SetProjection(srs.ExportToWkt())
        mem.GetRasterBand(1).WriteArray(feet.astype(np.float32))

        lines = ds.CreateLayer("contours", srs, ogr.wkbLineString)
        lines.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
        lines.CreateField(ogr.FieldDefn("elev_ft", ogr.OFTReal))
        lines.StartTransaction()
        gdal.ContourGenerate(mem.GetRasterBand(1), float(interval_ft), 0.0,
                             [], 0, 0.0, lines, 0, 1)
        lines.CommitTransaction()
        count = lines.GetFeatureCount()
        mem = None
    ds = None
    return count


# --------------------------------------------------------------------------
# Styling for the layers that get added to the project
# --------------------------------------------------------------------------
def style_area(layer):
    layer.renderer().setSymbol(QgsFillSymbol.createSimple({
        "color": "0,0,0,0", "outline_color": "220,40,40,255",
        "outline_width": "0.6", "outline_style": "dash"}))


def style_contours(layer, index_ft):
    index_ft = int(round(index_ft))

    def line(width, alpha):
        return QgsLineSymbol.createSimple({
            "line_color": "140,90,40,{}".format(alpha), "line_width": str(width)})

    # Like a paper topo: zoomed out you only see the index lines, so steep
    # ground doesn't turn into a solid brown sheet.
    root = QgsRuleBasedRenderer.Rule(None)
    root.appendChild(QgsRuleBasedRenderer.Rule(
        line(0.45, 230), maximumScale=0, minimumScale=INDEX_SCALE,
        filterExp='"elev_ft" % {} = 0'.format(index_ft),
        label="Index ({} ft)".format(index_ft)))
    root.appendChild(QgsRuleBasedRenderer.Rule(
        line(0.2, 150), maximumScale=0, minimumScale=CONTOUR_SCALE,
        filterExp='"elev_ft" % {} <> 0'.format(index_ft),
        label="Contour"))
    layer.setRenderer(QgsRuleBasedRenderer(root))

    pal = QgsPalLayerSettings()
    pal.fieldName = ('CASE WHEN "elev_ft" % {} = 0 '
                     'THEN to_string(to_int("elev_ft")) END').format(index_ft)
    pal.isExpression = True
    pal.placement = CURVED_LABELS
    pal.scaleVisibility = True
    pal.minimumScale = CONTOUR_SCALE
    pal.maximumScale = 0
    fmt = QgsTextFormat()
    fmt.setSize(8)
    fmt.setColor(QColor(110, 70, 30))
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.8)
    buf.setColor(QColor(255, 255, 255, 220))
    fmt.setBuffer(buf)
    pal.setFormat(fmt)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


# --------------------------------------------------------------------------
# The algorithm
# --------------------------------------------------------------------------
class OfflineAreaPackage(QgsProcessingAlgorithm):
    TRIP_LAYERS = "TRIP_LAYERS"
    SELECTED_ONLY = "SELECTED_ONLY"
    PAD_MI = "PAD_MI"
    BASEMAPS = "BASEMAPS"
    ZOOM_MIN = "ZOOM_MIN"
    ZOOM_MAX = "ZOOM_MAX"
    CONTOURS = "CONTOURS"
    INTERVAL_FT = "INTERVAL_FT"
    FOLDER = "FOLDER"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"

    def __init__(self):
        super().__init__()
        self._to_load = []
        self._group = ""
        self._index_ft = 200

    def name(self):
        return "offline_area_package"

    def displayName(self):
        return "Offline area package (basemaps + contours)"

    def group(self):
        return "Hiking"

    def groupId(self):
        return "hiking"

    def shortHelpString(self):
        return (
            "Draws a box around the trip layers you pick, pads it by the "
            "distance you set, then saves the ticked basemaps as MBTiles, an "
            "elevation model, and contour lines for that box into a "
            "'<trip>_offline' folder. Everything is added to the project in "
            "one group.\n\n"
            "Basemaps: click the '...' button and tick the layers to save. "
            "They must be XYZ tile layers in the project (add them from the "
            "Browser's XYZ Tiles first). Their original tiles are copied, so "
            "the offline copy looks exactly like the online map. The Mapzen "
            "terrain layer is skipped automatically; it's used for the "
            "contours instead.\n\n"
            "Zoom: each level up is about 4x the tiles. 16 is street-level "
            "detail; OpenStreetMap is capped at 16 to follow its tile usage "
            "policy.\n\n"
            "The saved DEM can be used as the 'Elevation raster' in Route "
            "stats and in the Elevation Profile panel when you're offline."
        )

    def createInstance(self):
        return OfflineAreaPackage()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.TRIP_LAYERS, "Trip layers (points and/or lines)",
            layerType=ANY_VECTOR))
        self.addParameter(QgsProcessingParameterBoolean(
            self.SELECTED_ONLY,
            "Use only selected features (in layers that have a selection)",
            defaultValue=False))
        self.addParameter(QgsProcessingParameterNumber(
            self.PAD_MI, "Extra distance around the box (miles)",
            type=NUM_DOUBLE, defaultValue=2.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.BASEMAPS, "Basemaps to download (click ... and tick them)",
            layerType=RASTER, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.ZOOM_MIN, "Basemap min zoom", type=NUM_INT,
            defaultValue=10, minValue=0, maxValue=19))
        self.addParameter(QgsProcessingParameterNumber(
            self.ZOOM_MAX, "Basemap max zoom", type=NUM_INT,
            defaultValue=16, minValue=0, maxValue=19))
        self.addParameter(QgsProcessingParameterBoolean(
            self.CONTOURS, "Make contour lines", defaultValue=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.INTERVAL_FT, "Contour interval (ft)", type=NUM_DOUBLE,
            defaultValue=40.0, minValue=1.0))
        self.addParameter(QgsProcessingParameterFile(
            self.FOLDER,
            "Save in folder (optional; blank = next to the first trip layer)",
            behavior=FOLDER_BEHAVIOR, optional=True))
        self.addOutput(QgsProcessingOutputFolder(
            self.OUTPUT_FOLDER, "Package folder"))

    # ---------------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        trips = self.parameterAsLayerList(parameters, self.TRIP_LAYERS, context)
        selected_only = self.parameterAsBoolean(parameters, self.SELECTED_ONLY, context)
        pad_mi = self.parameterAsDouble(parameters, self.PAD_MI, context)
        basemaps = self.parameterAsLayerList(parameters, self.BASEMAPS, context) or []
        zmin = self.parameterAsInt(parameters, self.ZOOM_MIN, context)
        zmax = self.parameterAsInt(parameters, self.ZOOM_MAX, context)
        make_contours = self.parameterAsBoolean(parameters, self.CONTOURS, context)
        interval_ft = self.parameterAsDouble(parameters, self.INTERVAL_FT, context)
        base = self.parameterAsFile(parameters, self.FOLDER, context)

        if not trips:
            raise QgsProcessingException("Pick at least one trip layer.")
        if zmin > zmax:
            zmin, zmax = zmax, zmin

        # ---- 1. Box around the trip, padded --------------------------------
        merc = QgsCoordinateReferenceSystem("EPSG:3857")
        tctx = context.transformContext()
        box = None
        for lyr in trips:
            if selected_only and lyr.selectedFeatureCount() > 0:
                ext = lyr.boundingBoxOfSelected()
            else:
                lyr.updateExtents()
                ext = lyr.extent()
            if ext.isNull() or not all(map(math.isfinite, (
                    ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()))):
                feedback.pushWarning("{} has no features; skipped.".format(lyr.name()))
                continue
            r = QgsCoordinateTransform(lyr.crs(), merc, tctx).transformBoundingBox(ext)
            if box is None:
                box = QgsRectangle(r)
            else:
                box.combineExtentWith(r)
        if box is None:
            raise QgsProcessingException("The trip layers have no features.")

        lat = merc_y_to_lat(box.center().y())
        scale = 1.0 / math.cos(math.radians(lat))   # true metres -> Mercator units
        pad = max(pad_mi * M_PER_MI, 250.0) * scale
        bbox = (box.xMinimum() - pad, box.yMinimum() - pad,
                box.xMaximum() + pad, box.yMaximum() + pad)
        feedback.pushInfo("Area: {:.1f} x {:.1f} mi".format(
            (bbox[2] - bbox[0]) / scale / M_PER_MI,
            (bbox[3] - bbox[1]) / scale / M_PER_MI))

        # ---- Output folder -------------------------------------------------
        trip_name = trips[0].name()
        if not base:
            src = trips[0].source().split("|")[0]
            base = os.path.dirname(src) if os.path.isfile(src) \
                else os.path.expanduser("~")
        pkg = os.path.join(base, safe_name(trip_name) + "_offline")
        os.makedirs(pkg, exist_ok=True)
        feedback.pushInfo("Saving to {}".format(pkg))

        self._to_load = []
        self._group = "Offline: {}".format(trip_name)
        self._index_ft = interval_ft * INDEX_EVERY

        steps = len(basemaps) + 2
        multi = QgsProcessingMultiStepFeedback(steps, feedback)
        step = 0

        # ---- 2. Basemaps -> MBTiles ---------------------------------------
        saved_maps = []
        for lyr in basemaps:
            if feedback.isCanceled():
                break
            multi.setCurrentStep(step)
            step += 1
            src = lyr.source().lower()
            if "terrarium" in src:
                feedback.pushInfo("Skipping {}: it's elevation data, used for "
                                  "the contours instead.".format(lyr.name()))
                continue
            info = xyz_info(lyr)
            if info is None:
                feedback.pushWarning(
                    "Skipping {}: only XYZ tile basemaps (like OpenStreetMap, "
                    "Esri or USGS from the Browser's XYZ Tiles) can be "
                    "downloaded.".format(lyr.name()))
                continue
            z_hi = min(zmax, info[2])
            if "openstreetmap.org" in src and z_hi > 16:
                feedback.pushWarning(
                    "{}: capped at zoom 16 (OpenStreetMap's tile policy doesn't "
                    "allow bulk downloads at 17+).".format(lyr.name()))
                z_hi = 16
            z_lo = min(max(zmin, info[1]), z_hi)
            n = tile_count(bbox, z_lo, z_hi)
            if n > MAX_BASEMAP_TILES:
                raise QgsProcessingException(
                    "{} would need {:,} tiles at zoom {}-{}. Lower the max zoom "
                    "or the padding.".format(lyr.name(), n, z_lo, z_hi))
            feedback.pushInfo("{}: {:,} tiles, zoom {}-{}".format(
                lyr.name(), n, z_lo, z_hi))
            out = os.path.join(pkg, safe_name(lyr.name()) + ".mbtiles")
            threads = OSM_THREADS if "openstreetmap.org" in src else DOWNLOAD_THREADS
            got, missing = download_mbtiles(info, bbox, z_lo, z_hi, out,
                                            lyr.name(), multi, threads)
            if got == 0:
                feedback.reportError(
                    "{}: no tiles came back (offline, or the server refused "
                    "the downloads).".format(lyr.name()), False)
                continue
            if missing:
                feedback.pushWarning("{}: {:,} of {:,} tiles weren't available."
                                     .format(lyr.name(), missing, n))
            saved_maps.append((out, "{} (offline)".format(lyr.name())))

        # ---- 3. DEM from terrain tiles ------------------------------------
        if feedback.isCanceled():
            return {}
        multi.setCurrentStep(step)
        step += 1
        z = DEM_ZOOM
        while z > 10 and tile_count(bbox, z, z) > MAX_DEM_TILES:
            z -= 1
        feedback.pushInfo("Elevation: {} terrain tiles at zoom {}".format(
            tile_count(bbox, z, z), z))
        cache = os.path.join(QgsApplication.qgisSettingsDirPath(),
                             "cache", "terrarium_tiles")
        dem_path = os.path.join(pkg, "elevation_dem.tif")
        dem, gt = build_dem(bbox, z, cache, dem_path, multi)

        # ---- 4. Area box + contours ---------------------------------------
        multi.setCurrentStep(step)
        gpkg = os.path.join(pkg, "area_and_contours.gpkg")
        n_lines = write_area_and_contours(
            gpkg, bbox, dem, gt, interval_ft if make_contours else None)
        if make_contours:
            feedback.pushInfo("Contours: {:,} lines every {:g} ft".format(
                n_lines, interval_ft))

        # Layers to add (top to bottom) once the run finishes
        self._to_load.append(("area", gpkg + "|layername=area", "Area"))
        if make_contours and n_lines:
            self._to_load.append(("contours", gpkg + "|layername=contours",
                                  "Contours ({:g} ft)".format(interval_ft)))
        for i, (path, title) in enumerate(saved_maps):
            self._to_load.append(("basemap_on" if i == 0 else "basemap", path, title))
        self._to_load.append(("dem", dem_path, "Elevation (DEM, metres)"))

        return {self.OUTPUT_FOLDER: pkg}

    # ---------------------------------------------------------------------
    def postProcessAlgorithm(self, context, feedback):
        if not self._to_load:
            return {}
        project = context.project() or QgsProject.instance()
        root = project.layerTreeRoot()
        old = root.findGroup(self._group)
        if old is not None:
            root.removeChildNode(old)
        # Put the group under your own points/lines (so the offline imagery
        # doesn't cover them) but above any online basemaps.
        index = 0
        for i, node in enumerate(root.children()):
            if QgsLayerTree.isLayer(node):
                layers = [node.layer()]
            elif QgsLayerTree.isGroup(node):
                layers = [n.layer() for n in node.findLayers()]
            else:
                layers = []
            if any(isinstance(l, QgsVectorLayer) for l in layers):
                index = i + 1
        group = root.insertGroup(index, self._group)

        for kind, path, title in self._to_load:
            if kind in ("area", "contours"):
                layer = QgsVectorLayer(path, title, "ogr")
            else:
                layer = QgsRasterLayer(path, title, "gdal")
            if not layer.isValid():
                feedback.pushWarning("Couldn't open {}".format(path))
                continue
            if kind == "area":
                style_area(layer)
            elif kind == "contours":
                style_contours(layer, self._index_ft)
            elif kind == "dem":
                try:
                    layer.elevationProperties().setEnabled(True)
                except AttributeError:
                    pass
            project.addMapLayer(layer, False)
            node = group.addLayer(layer)
            node.setItemVisibilityChecked(kind not in ("basemap", "dem"))
        return {}
