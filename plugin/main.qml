// Route Elevation Profile - QField plugin
// =======================================
// Tap the mountain button, then tap any line on the map (or open a line in
// QField's feature form first) to get an interactive elevation profile:
//   - drag along the chart to see distance, elevation and grade, with a
//     marker that follows on the map
//   - pinch the chart to zoom in on a section, double-tap to reset
//   - when GPS is on and you're on the route, it shows where you are and
//     the distance / climb left to the end
//
// Elevation comes from a DEM raster in the project (for example the
// "Elevation (DEM)" layer from the Offline area package script), so it
// works with no signal.
//
// Use as an app-wide plugin (main.qml + metadata.txt + icon.svg in a zip)
// or as a project plugin (copy this file next to your project and rename it
// to match, e.g. caliente.qgz -> caliente.qml).

import QtQuick
import QtQuick.Controls
import org.qfield
import org.qgis
import Theme

Item {
  id: plugin

  // ---------------------------------------------------------------- state
  readonly property string settingsKey: "/QField/plugins/route_elevation_profile/"
  property bool metric: false
  property real gainThreshold: 3.0           // metres; smaller wiggles are DEM noise
  property string preferredDem: ""           // layer name, "" = automatic

  property var mainWindow: iface.mainWindow()
  property var mapCanvas: iface.mapCanvas()
  property var pointHandler: iface.findItemByObjectName("pointHandler")

  property bool picking: false
  property bool loading: false
  property real loadProgress: 0
  property string message: ""

  property string routeName: ""
  property string routeWkt: ""
  property var lons: []
  property var lats: []
  property var dists: []                     // metres along the route
  property var elevs: []                     // metres; NaN where there's no data
  property real totalDist: 0
  property var stats: null                   // {gain, loss, high, low}

  property string demId: ""
  property string demName: ""
  property int sampleIndex: 0

  property int cursorIndex: -1
  property var cursorMapPoint: GeometryUtils.point(0, 0)

  property int gpsIndex: -1
  property real gpsOffRoute: -1
  property var gpsAhead: null                // {dist, gain, loss}

  property real viewStart: 0
  property real viewEnd: 1

  // keep the chart in sync with anything it draws
  onMetricChanged: chart.requestPaint()
  onViewStartChanged: chart.requestPaint()
  onViewEndChanged: chart.requestPaint()
  onCursorIndexChanged: chart.requestPaint()
  onGpsIndexChanged: chart.requestPaint()
  onElevsChanged: chart.requestPaint()

  // ------------------------------------------------------------ helpers
  function ft(m) {
    return m * 3.28084;
  }

  function group(n) {
    const neg = n < 0;
    let s = Math.abs(Math.round(n)).toString();
    let out = "";
    while (s.length > 3) {
      out = "," + s.slice(-3) + out;
      s = s.slice(0, -3);
    }
    return (neg ? "-" : "") + s + out;
  }

  function fmtElev(m) {
    if (isNaN(m)) {
      return "–";
    }
    return metric ? group(m) + " m" : group(ft(m)) + " ft";
  }

  function fmtDist(m) {
    if (metric) {
      return m >= 1000 ? (m / 1000).toFixed(m >= 10000 ? 1 : 2) + " km" : Math.round(m) + " m";
    }
    const mi = m / 1609.344;
    return mi >= 0.1 ? mi.toFixed(mi >= 10 ? 1 : 2) + " mi" : group(ft(m)) + " ft";
  }

  function haversine(lon1, lat1, lon2, lat2) {
    const r = 6371008.8;
    const toRad = Math.PI / 180;
    const dLat = (lat2 - lat1) * toRad;
    const dLon = (lon2 - lon1) * toRad;
    const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) + Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * r * Math.asin(Math.min(1, Math.sqrt(a)));
  }

  // "LINESTRING (x y, ...)" / "MultiLineString Z ((...),(...))" -> [[[lon, lat], ...], ...]
  function parseWkt(wkt) {
    const parts = [];
    const re = /\(([^()]+)\)/g;
    let m;
    while ((m = re.exec(wkt)) !== null) {
      const pts = [];
      const coords = m[1].split(",");
      for (let i = 0; i < coords.length; i++) {
        const v = coords[i].trim().split(/\s+/);
        const x = parseFloat(v[0]);
        const y = parseFloat(v[1]);
        if (!isNaN(x) && !isNaN(y)) {
          pts.push([x, y]);
        }
      }
      if (pts.length >= 2) {
        parts.push(pts);
      }
    }
    return parts;
  }

  // Climb and descent, ignoring wiggles under the threshold (same method as
  // the Route stats script). gain - loss always equals end - start.
  function gainLoss(values, threshold) {
    let gain = 0, loss = 0, ref = NaN, last = NaN;
    for (let i = 0; i < values.length; i++) {
      const e = values[i];
      if (isNaN(e)) {
        continue;
      }
      last = e;
      if (isNaN(ref)) {
        ref = e;
        continue;
      }
      const d = e - ref;
      if (d >= threshold && d > 0) {
        gain += d;
        ref = e;
      } else if (d <= -threshold && d < 0) {
        loss -= d;
        ref = e;
      }
    }
    if (!isNaN(ref)) {
      const d = last - ref;
      if (d > 0) {
        gain += d;
      } else {
        loss -= d;
      }
    }
    return {
      "gain": gain,
      "loss": loss
    };
  }

  function niceStep(span, count) {
    const raw = span / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    const n = raw / mag;
    return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
  }

  function indexAtDistance(d) {
    let lo = 0, hi = dists.length - 1;
    if (hi < 0) {
      return -1;
    }
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (dists[mid] < d) {
        lo = mid + 1;
      } else {
        hi = mid;
      }
    }
    if (lo > 0 && Math.abs(dists[lo - 1] - d) < Math.abs(dists[lo] - d)) {
      lo--;
    }
    return lo;
  }

  // Grade (%) around sample i, measured over roughly +/- 40 m
  function gradeAt(i) {
    const win = 40;
    let a = i, b = i;
    while (a > 0 && dists[i] - dists[a] < win) {
      a--;
    }
    while (b < dists.length - 1 && dists[b] - dists[i] < win) {
      b++;
    }
    while (a < i && isNaN(elevs[a])) {
      a++;
    }
    while (b > i && isNaN(elevs[b])) {
      b--;
    }
    const run = dists[b] - dists[a];
    if (run <= 0 || isNaN(elevs[a]) || isNaN(elevs[b])) {
      return NaN;
    }
    return 100 * (elevs[b] - elevs[a]) / run;
  }

  function toast(text) {
    mainWindow.displayToast(text);
  }

  // -------------------------------------------------------- settings
  function loadSettings() {
    try {
      metric = settings.valueBool(settingsKey + "metric", false);
      gainThreshold = parseFloat(settings.value(settingsKey + "gainThreshold", 3.0));
      if (isNaN(gainThreshold)) {
        gainThreshold = 3.0;
      }
      preferredDem = String(settings.value(settingsKey + "dem", ""));
    } catch (e) {
      console.log("Route Elevation Profile: couldn't read settings: " + e);
    }
  }

  function saveSettings() {
    try {
      settings.setValue(settingsKey + "metric", metric);
      settings.setValue(settingsKey + "gainThreshold", gainThreshold);
      settings.setValue(settingsKey + "dem", preferredDem);
    } catch (e) {
      console.log("Route Elevation Profile: couldn't save settings: " + e);
    }
  }

  // QField's plugin manager shows a settings button when this exists
  function configure() {
    settingsPopup.open();
  }

  // ------------------------------------------------------ expressions
  ExpressionEvaluator {
    id: featureEvaluator
    project: qgisProject
  }

  ExpressionEvaluator {
    id: plainEvaluator
    project: qgisProject
  }

  function evalFeature(layer, feature, expression) {
    featureEvaluator.layer = layer;
    featureEvaluator.feature = feature;
    return String(featureEvaluator.evaluate(expression));
  }

  function evalPlain(expression) {
    return String(plainEvaluator.evaluate(expression));
  }

  // ------------------------------------------------------- DEM layers
  function demCandidates() {
    const out = [];
    const layers = ProjectUtils.mapLayers(qgisProject);
    for (const id in layers) {
      const layer = layers[id];
      if (!layer || layer.type !== Qgis.LayerType.Raster) {
        continue;
      }
      let score = 0;
      if (/dem|elev|terrain|dtm|dsm|height|lidar|srtm|3dep|copernicus/i.test(layer.name)) {
        score += 2;
      }
      if (/mapzen|terrarium|tiles/i.test(layer.name)) {
        score -= 1;                          // online tiles: last resort
      }
      if (preferredDem !== "" && layer.name === preferredDem) {
        score += 100;
      }
      out.push({
        "id": id,
        "name": layer.name,
        "score": score
      });
    }
    out.sort((a, b) => b.score - a.score);
    return out;
  }

  function rasterExpression(id, wgs84Wkt) {
    return "with_variable('g', transform(geom_from_wkt('" + wgs84Wkt + "'), 'EPSG:4326', layer_property('" + id + "', 'crs')), " + "array_to_string(array_foreach(generate_series(1, num_points(@g)), " + "coalesce(to_string(raster_value('" + id + "', 1, point_n(@g, @element))), 'x')), ','))";
  }

  // First raster that actually has data along the route wins
  function pickDem() {
    const candidates = demCandidates();
    const probes = [];
    const n = lons.length;
    for (let k = 1; k <= 5; k++) {
      const i = Math.min(n - 1, Math.floor(n * k / 6));
      probes.push("(" + lons[i].toFixed(7) + " " + lats[i].toFixed(7) + ")");
    }
    const wkt = "MULTIPOINT(" + probes.join(",") + ")";
    for (let c = 0; c < candidates.length; c++) {
      const values = evalPlain(rasterExpression(candidates[c].id, wkt)).split(",");
      for (let v = 0; v < values.length; v++) {
        if (!isNaN(parseFloat(values[v]))) {
          return candidates[c];
        }
      }
    }
    return null;
  }

  // ---------------------------------------------------- picking routes
  function startPicking() {
    if (picking) {
      stopPicking();
      return;
    }
    // A line already open in QField's feature form? Use it straight away.
    try {
      const form = iface.findItemByObjectName("featureForm");
      if (form && form.visible && form.selection && form.selection.focusedLayer) {
        const layer = form.selection.focusedLayer;
        if (layer.type === Qgis.LayerType.Vector && layer.geometryType() === Qgis.GeometryType.Line) {
          loadRoute(layer, form.selection.focusedFeature);
          return;
        }
      }
    } catch (e) {
      console.log("Route Elevation Profile: feature form not available: " + e);
    }
    picking = true;
    pointHandler.registerHandler("route_elevation_profile", onMapTap, 100);
    toast(qsTr("Tap a route on the map"));
  }

  function stopPicking() {
    if (picking) {
      pointHandler.deregisterHandler("route_elevation_profile");
      picking = false;
    }
  }

  function onMapTap(point, type, interactionType) {
    if (interactionType !== "clicked") {
      return false;
    }
    const hit = findLineAt(point);
    if (hit) {
      stopPicking();
      loadRoute(hit.layer, hit.feature);
    } else {
      toast(qsTr("No route there. Tap right on a line, or tap the mountain button to cancel."));
    }
    return true;
  }

  function findLineAt(point) {
    const ms = mapCanvas.mapSettings;
    const destCrs = ms.destinationCrs;
    const p = ms.screenToCoordinate(point);
    const tol = 16 * ms.mapUnitsPerPoint;
    const rect = GeometryUtils.createRectangleFromPoints(GeometryUtils.point(p.x - tol, p.y - tol), GeometryUtils.point(p.x + tol, p.y + tol));
    let best = null;
    const layers = ProjectUtils.mapLayers(qgisProject);
    for (const id in layers) {
      const layer = layers[id];
      if (!layer || layer.type !== Qgis.LayerType.Vector || layer.geometryType() !== Qgis.GeometryType.Line) {
        continue;
      }
      if (/contour/i.test(layer.name)) {
        continue;
      }
      const lp = GeometryUtils.reprojectPoint(p, destCrs, layer.crs);
      const lq = GeometryUtils.reprojectPoint(GeometryUtils.point(p.x + tol, p.y), destCrs, layer.crs);
      const ltol = Math.sqrt((lq.x - lp.x) * (lq.x - lp.x) + (lq.y - lp.y) * (lq.y - lp.y));
      const lrect = GeometryUtils.reprojectRectangle(rect, destCrs, layer.crs);
      const it = LayerUtils.createFeatureIteratorFromRectangle(layer, lrect);
      let checked = 0;
      while (it.hasNext() && checked < 300) {
        const feature = it.next();
        checked++;
        const d = parseFloat(evalFeature(layer, feature, "distance($geometry, geom_from_wkt('POINT(" + lp.x + " " + lp.y + ")'))"));
        if (!isNaN(d) && d <= ltol && (best === null || d / ltol < best.score)) {
          best = {
            "layer": layer,
            "feature": feature,
            "score": d / ltol
          };
        }
      }
      it.close();
    }
    return best;
  }

  function routeTitle(layer, feature) {
    const fieldsToTry = ["name", "Name", "NAME", "title", "Title", "trail", "route"];
    for (let i = 0; i < fieldsToTry.length; i++) {
      try {
        const v = feature.attribute(fieldsToTry[i]);
        if (v !== undefined && v !== null && String(v).trim() !== "" && String(v) !== "NULL") {
          return String(v);
        }
      } catch (e) {}
    }
    try {
      if (layer.displayExpression) {
        const v = evalFeature(layer, feature, layer.displayExpression);
        if (v !== "" && v !== "NULL" && isNaN(Number(v))) {
          return v;
        }
      }
    } catch (e) {}
    return layer.name;
  }

  // ------------------------------------------------- building a profile
  function loadRoute(layer, feature) {
    const authid = layer.crs && layer.crs.authid ? layer.crs.authid : "";
    const source = authid !== "" ? "'" + authid + "'" : "@layer_crs";
    const wkt = evalFeature(layer, feature, "geom_to_wkt(transform($geometry, " + source + ", 'EPSG:4326'), 7)");
    const parts = parseWkt(wkt);
    if (parts.length === 0) {
      toast(qsTr("Couldn't read that line's geometry."));
      return;
    }
    routeName = routeTitle(layer, feature);
    routeWkt = wkt;
    buildSamples(parts);
    showRouteOnMap();
    panel.visible = true;
    startSampling();
  }

  // Points every ~10-50 m along the route (about 1,500 at most)
  function buildSamples(parts) {
    let length = 0;
    for (let p = 0; p < parts.length; p++) {
      for (let i = 1; i < parts[p].length; i++) {
        length += haversine(parts[p][i - 1][0], parts[p][i - 1][1], parts[p][i][0], parts[p][i][1]);
      }
    }
    const step = Math.max(10, Math.min(50, length / 1500));
    const lo = [], la = [], di = [];
    let cum = 0;
    for (let p = 0; p < parts.length; p++) {
      const pts = parts[p];
      lo.push(pts[0][0]);
      la.push(pts[0][1]);
      di.push(cum);
      for (let i = 1; i < pts.length; i++) {
        const seg = haversine(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]);
        const steps = Math.max(1, Math.ceil(seg / step));
        for (let s = 1; s <= steps; s++) {
          const t = s / steps;
          lo.push(pts[i - 1][0] + t * (pts[i][0] - pts[i - 1][0]));
          la.push(pts[i - 1][1] + t * (pts[i][1] - pts[i - 1][1]));
          di.push(cum + t * seg);
        }
        cum += seg;
      }
    }
    lons = lo;
    lats = la;
    dists = di;
    totalDist = cum;
    elevs = [];
    stats = null;
    cursorIndex = -1;
    gpsIndex = -1;
    gpsOffRoute = -1;
    gpsAhead = null;
    viewStart = 0;
    viewEnd = Math.max(1, cum);
  }

  function startSampling() {
    message = "";
    const dem = pickDem();
    if (!dem) {
      loading = false;
      message = qsTr("No elevation data found for this route. Add a DEM raster to the project (for example the offline package's Elevation (DEM) layer).");
      chart.requestPaint();
      return;
    }
    demId = dem.id;
    demName = dem.name;
    const blank = new Array(lons.length);
    for (let i = 0; i < blank.length; i++) {
      blank[i] = NaN;
    }
    elevs = blank;
    sampleIndex = 0;
    loadProgress = 0;
    loading = true;
    samplingTimer.start();
  }

  Timer {
    id: samplingTimer
    interval: 1
    repeat: true
    onTriggered: plugin.sampleNextChunk()
  }

  function sampleNextChunk() {
    const chunk = 200;
    const end = Math.min(sampleIndex + chunk, lons.length);
    const pts = [];
    for (let i = sampleIndex; i < end; i++) {
      pts.push("(" + lons[i].toFixed(7) + " " + lats[i].toFixed(7) + ")");
    }
    const values = evalPlain(rasterExpression(demId, "MULTIPOINT(" + pts.join(",") + ")")).split(",");
    const e = elevs;
    for (let i = sampleIndex, k = 0; i < end; i++, k++) {
      const v = parseFloat(values[k]);
      e[i] = isNaN(v) || v < -1000 ? NaN : v;   // treat nodata sentinels as gaps
    }
    sampleIndex = end;
    loadProgress = lons.length ? end / lons.length : 1;
    if (end >= lons.length) {
      samplingTimer.stop();
      elevs = e;
      finishProfile();
    } else {
      chart.requestPaint();
    }
  }

  function finishProfile() {
    loading = false;
    let high = -Infinity, low = Infinity, valid = 0;
    for (let i = 0; i < elevs.length; i++) {
      const v = elevs[i];
      if (!isNaN(v)) {
        valid++;
        high = Math.max(high, v);
        low = Math.min(low, v);
      }
    }
    if (valid < 2) {
      message = qsTr("The elevation layer (%1) has no data along this route.").arg(demName);
      stats = null;
    } else {
      const gl = gainLoss(elevs, gainThreshold);
      stats = {
        "gain": gl.gain,
        "loss": gl.loss,
        "high": high,
        "low": low
      };
    }
    updateGps();
    chart.requestPaint();
  }

  function recomputeStats() {
    if (stats) {
      const gl = gainLoss(elevs, gainThreshold);
      stats = {
        "gain": gl.gain,
        "loss": gl.loss,
        "high": stats.high,
        "low": stats.low
      };
      updateGps();
    }
  }

  // --------------------------------------------------------- map items
  function showRouteOnMap() {
    routeRenderer.geometryWrapper.crs = CoordinateReferenceSystemUtils.wgs84Crs();
    routeRenderer.geometryWrapper.qgsGeometry = GeometryUtils.createGeometryFromWkt(routeWkt);
  }

  function setCursor(i) {
    cursorIndex = i;
    if (i >= 0) {
      cursorMapPoint = GeometryUtils.reprojectPoint(GeometryUtils.point(lons[i], lats[i]), CoordinateReferenceSystemUtils.wgs84Crs(), mapCanvas.mapSettings.destinationCrs);
    }
    chart.requestPaint();
  }

  // After scrubbing, pan the map if the marker is hidden behind the panel
  function ensureCursorVisible() {
    if (cursorIndex < 0) {
      return;
    }
    const sp = cursorOnMap.screenPoint;
    const visibleBottom = mapCanvas.height - panel.height - 16;
    if (sp.x < 16 || sp.x > mapCanvas.width - 16 || sp.y < 16 || sp.y > visibleBottom) {
      mapCanvas.mapSettings.center = cursorMapPoint;
    }
  }

  function zoomToRoute() {
    if (lons.length === 0) {
      return;
    }
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (let i = 0; i < lons.length; i++) {
      minX = Math.min(minX, lons[i]);
      maxX = Math.max(maxX, lons[i]);
      minY = Math.min(minY, lats[i]);
      maxY = Math.max(maxY, lats[i]);
    }
    const padX = Math.max((maxX - minX) * 0.1, 0.002);
    const padY = Math.max((maxY - minY) * 0.1, 0.002);
    // Extra room at the bottom so the route isn't hidden under the panel
    const share = Math.min(0.6, panel.height / Math.max(1, mapCanvas.height));
    const extraBottom = (maxY - minY + 2 * padY) * share / (1 - share);
    const rect = GeometryUtils.createRectangleFromPoints(GeometryUtils.point(minX - padX, minY - padY - extraBottom), GeometryUtils.point(maxX + padX, maxY + padY));
    mapCanvas.mapSettings.extent = GeometryUtils.reprojectRectangle(rect, CoordinateReferenceSystemUtils.wgs84Crs(), mapCanvas.mapSettings.destinationCrs);
  }

  function closeProfile() {
    samplingTimer.stop();
    stopPicking();
    panel.visible = false;
    loading = false;
    cursorIndex = -1;
    routeRenderer.geometryWrapper.qgsGeometry = GeometryUtils.createGeometryFromWkt("");
  }

  // ---------------------------------------------------------------- GPS
  function updateGps() {
    gpsIndex = -1;
    gpsOffRoute = -1;
    gpsAhead = null;
    if (!stats || lons.length === 0) {
      return;
    }
    let info = null;
    try {
      const positioning = iface.positioning();
      if (!positioning || !positioning.active) {
        return;
      }
      info = positioning.positionInformation;
    } catch (e) {
      return;
    }
    if (!info || !info.latitudeValid || !info.longitudeValid) {
      return;
    }
    const lat = info.latitude, lon = info.longitude;
    const kx = Math.cos(lat * Math.PI / 180) * 111320, ky = 110574;
    let bestI = -1, bestD = Infinity;
    for (let i = 0; i < lons.length; i++) {
      const dx = (lons[i] - lon) * kx, dy = (lats[i] - lat) * ky;
      const d = dx * dx + dy * dy;
      if (d < bestD) {
        bestD = d;
        bestI = i;
      }
    }
    const offRoute = Math.sqrt(bestD);
    if (offRoute > 150) {
      gpsOffRoute = offRoute;
      chart.requestPaint();
      return;
    }
    gpsIndex = bestI;
    const ahead = gainLoss(elevs.slice(bestI), gainThreshold);
    gpsAhead = {
      "dist": totalDist - dists[bestI],
      "gain": ahead.gain,
      "loss": ahead.loss
    };
    chart.requestPaint();
  }

  Timer {
    interval: 3000
    repeat: true
    running: panel.visible && plugin.stats !== null
    onTriggered: plugin.updateGps()
  }

  // ------------------------------------------------- toolbar button
  QfToolButton {
    id: profileButton
    iconSource: Theme.getThemeVectorIcon("ic_elevation_white_24dp")
    iconColor: plugin.picking ? Theme.mainColor : Theme.toolButtonColor
    bgcolor: plugin.picking ? Theme.toolButtonColor : Theme.toolButtonBackgroundColor
    round: true

    onClicked: plugin.startPicking()
    onPressAndHold: settingsPopup.open()
  }

  // ------------------------------------------------- items on the map
  GeometryRenderer {
    id: routeRenderer
    parent: plugin.mapCanvas
    anchors.fill: parent
    mapSettings: plugin.mapCanvas.mapSettings
    color: "#e8590c"
    lineWidth: 6
    visible: panel.visible
  }

  MapToScreen {
    id: cursorOnMap
    mapSettings: plugin.mapCanvas.mapSettings
    mapPoint: plugin.cursorMapPoint
  }

  Rectangle {
    parent: plugin.mapCanvas
    visible: panel.visible && plugin.cursorIndex >= 0
    width: 22
    height: 22
    radius: 11
    x: cursorOnMap.screenPoint.x - width / 2
    y: cursorOnMap.screenPoint.y - height / 2
    color: "#e8590c"
    border.color: "white"
    border.width: 3
  }

  // --------------------------------------------------- profile panel
  Rectangle {
    id: panel
    parent: plugin.mainWindow.contentItem
    visible: false
    z: 1000

    readonly property real bottomInset: plugin.mainWindow.sceneBottomMargin !== undefined ? plugin.mainWindow.sceneBottomMargin : 0

    width: Math.min(parent.width, 760)
    height: Math.min(Math.max(parent.height * 0.4, 230), 360) + bottomInset
    anchors.bottom: parent.bottom
    anchors.horizontalCenter: parent.horizontalCenter
    color: Theme.mainBackgroundColor
    radius: 12

    // swallow touches so they don't reach the map underneath
    MouseArea {
      anchors.fill: parent
    }

    // header: name + buttons
    Item {
      id: header
      anchors.top: parent.top
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.margins: 8
      height: 36

      Text {
        anchors.left: parent.left
        anchors.right: buttons.left
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: 4
        text: plugin.routeName
        font: Theme.strongFont
        color: Theme.mainTextColor
        elide: Text.ElideRight
      }

      Row {
        id: buttons
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        spacing: 4

        Button {
          text: plugin.metric ? "m" : "ft"
          flat: true
          width: 44
          height: 36
          onClicked: {
            plugin.metric = !plugin.metric;
            plugin.saveSettings();
            chart.requestPaint();
          }
        }
        Button {
          flat: true
          width: 40
          height: 36
          icon.source: Theme.getThemeVectorIcon("zoom_out_map_24dp")
          icon.color: Theme.mainTextColor
          onClicked: plugin.zoomToRoute()
        }
        Button {
          flat: true
          width: 40
          height: 36
          icon.source: Theme.getThemeVectorIcon("ic_tune_white_24dp")
          icon.color: Theme.mainTextColor
          onClicked: settingsPopup.open()
        }
        Button {
          flat: true
          width: 40
          height: 36
          icon.source: Theme.getThemeVectorIcon("ic_close_white_24dp")
          icon.color: Theme.mainTextColor
          onClicked: plugin.closeProfile()
        }
      }
    }

    // stats row (wraps on narrow screens)
    Flow {
      id: statsRow
      anchors.top: header.bottom
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.leftMargin: 12
      anchors.rightMargin: 12
      spacing: 16

      Repeater {
        model: [
          {
            "label": qsTr("Distance"),
            "value": plugin.fmtDist(plugin.totalDist)
          },
          {
            "label": qsTr("Gain"),
            "value": plugin.stats ? "↑ " + plugin.fmtElev(plugin.stats.gain) : "–"
          },
          {
            "label": qsTr("Loss"),
            "value": plugin.stats ? "↓ " + plugin.fmtElev(plugin.stats.loss) : "–"
          },
          {
            "label": qsTr("High"),
            "value": plugin.stats ? plugin.fmtElev(plugin.stats.high) : "–"
          },
          {
            "label": qsTr("Low"),
            "value": plugin.stats ? plugin.fmtElev(plugin.stats.low) : "–"
          }
        ]

        Column {
          Text {
            text: modelData.label
            font: Theme.tinyFont
            color: Theme.secondaryTextColor
          }
          Text {
            text: modelData.value
            font: Theme.tipFont
            color: Theme.mainTextColor
          }
        }
      }
    }

    // "from you" line when GPS is on
    Item {
      id: gpsLine
      anchors.top: statsRow.bottom
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.leftMargin: 12
      anchors.rightMargin: 12
      anchors.topMargin: 4
      height: gpsText.text === "" ? 0 : gpsText.implicitHeight

      Text {
        id: gpsText
        width: parent.width
        font: Theme.tinyFont
        color: Theme.positionColor
        elide: Text.ElideRight
        text: {
        if (plugin.gpsAhead) {
          return qsTr("From your position: %1 to go · ↑ %2 · ↓ %3").arg(plugin.fmtDist(plugin.gpsAhead.dist)).arg(plugin.fmtElev(plugin.gpsAhead.gain)).arg(plugin.fmtElev(plugin.gpsAhead.loss));
        }
        if (plugin.gpsOffRoute > 0) {
          return qsTr("You're %1 from this route").arg(plugin.fmtDist(plugin.gpsOffRoute));
        }
        return "";
      }
      }
    }

    // ------------------------------------------------------ the chart
    Canvas {
      id: chart
      anchors.top: gpsLine.bottom
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.bottom: parent.bottom
      anchors.topMargin: 6
      anchors.leftMargin: 6
      anchors.rightMargin: 8
      anchors.bottomMargin: 6 + panel.bottomInset

      readonly property real padL: 52
      readonly property real padR: 10
      readonly property real padT: 8
      readonly property real padB: 22
      readonly property real plotW: Math.max(1, width - padL - padR)
      readonly property real plotH: Math.max(1, height - padT - padB)

      property real yMin: 0
      property real yMax: 1

      function xFor(d) {
        return padL + (d - plugin.viewStart) / (plugin.viewEnd - plugin.viewStart) * plotW;
      }

      function yFor(e) {
        return padT + (1 - (e - yMin) / (yMax - yMin)) * plotH;
      }

      function distAt(x) {
        return plugin.viewStart + (x - padL) / plotW * (plugin.viewEnd - plugin.viewStart);
      }

      onWidthChanged: requestPaint()
      onHeightChanged: requestPaint()

      onPaint: {
        const ctx = getContext("2d");
        ctx.reset();
        const d = plugin.dists, e = plugin.elevs;
        if (d.length < 2 || e.length !== d.length) {
          return;
        }
        const i0 = Math.max(0, plugin.indexAtDistance(plugin.viewStart) - 1);
        const i1 = Math.min(d.length - 1, plugin.indexAtDistance(plugin.viewEnd) + 1);
        const toUnits = plugin.metric ? 1 : 3.28084;

        // vertical range from what's on screen
        let lo = Infinity, hi = -Infinity;
        for (let i = i0; i <= i1; i++) {
          if (!isNaN(e[i])) {
            lo = Math.min(lo, e[i]);
            hi = Math.max(hi, e[i]);
          }
        }
        if (lo === Infinity) {
          return;
        }
        const span = Math.max(hi - lo, 20);
        yMin = lo - span * 0.08;
        yMax = lo + span * 1.1;

        const textColor = Theme.secondaryTextColor;
        const gridColor = Qt.rgba(textColor.r, textColor.g, textColor.b, 0.18);
        const mainColor = Theme.mainColor;
        ctx.font = Math.round(Theme.tinyFont.pointSize > 0 ? Theme.tinyFont.pointSize * 1.35 : 11) + "px sans-serif";
        ctx.lineWidth = 1;

        // horizontal grid + elevation labels
        const yStep = plugin.niceStep((yMax - yMin) * toUnits, 4);
        ctx.fillStyle = textColor;
        ctx.strokeStyle = gridColor;
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        for (let v = Math.ceil(yMin * toUnits / yStep) * yStep; v <= yMax * toUnits; v += yStep) {
          const y = yFor(v / toUnits);
          ctx.beginPath();
          ctx.moveTo(padL, y);
          ctx.lineTo(padL + plotW, y);
          ctx.stroke();
          ctx.fillText(plugin.group(v), padL - 6, y);
        }

        // distance labels
        const distUnit = plugin.metric ? 1000 : 1609.344;
        const xStep = plugin.niceStep((plugin.viewEnd - plugin.viewStart) / distUnit, Math.max(2, Math.floor(plotW / 90)));
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        for (let v = Math.ceil(plugin.viewStart / distUnit / xStep) * xStep; v * distUnit <= plugin.viewEnd; v += xStep) {
          const x = xFor(v * distUnit);
          ctx.beginPath();
          ctx.moveTo(x, padT);
          ctx.lineTo(x, padT + plotH);
          ctx.stroke();
          const decimals = xStep < 0.1 ? 2 : xStep < 1 ? 1 : 0;
          ctx.fillText(v.toFixed(decimals) + (plugin.metric ? " km" : " mi"), x, padT + plotH + 4);
        }

        // profile: filled area + line, broken where there's no data
        ctx.save();
        ctx.beginPath();
        ctx.rect(padL, padT, plotW, plotH);
        ctx.clip();
        const stride = Math.max(1, Math.floor((i1 - i0) / (plotW * 2)));
        let runs = [], run = [];
        for (let i = i0; i <= i1; i += stride) {
          if (isNaN(e[i])) {
            if (run.length > 1) {
              runs.push(run);
            }
            run = [];
          } else {
            run.push([xFor(d[i]), yFor(e[i])]);
          }
        }
        if (run.length > 1) {
          runs.push(run);
        }
        ctx.fillStyle = Qt.rgba(mainColor.r, mainColor.g, mainColor.b, 0.22);
        for (let r = 0; r < runs.length; r++) {
          const pts = runs[r];
          ctx.beginPath();
          ctx.moveTo(pts[0][0], padT + plotH);
          for (let k = 0; k < pts.length; k++) {
            ctx.lineTo(pts[k][0], pts[k][1]);
          }
          ctx.lineTo(pts[pts.length - 1][0], padT + plotH);
          ctx.closePath();
          ctx.fill();
        }
        ctx.strokeStyle = mainColor;
        ctx.lineWidth = 2;
        ctx.lineJoin = "round";
        for (let r = 0; r < runs.length; r++) {
          const pts = runs[r];
          ctx.beginPath();
          ctx.moveTo(pts[0][0], pts[0][1]);
          for (let k = 1; k < pts.length; k++) {
            ctx.lineTo(pts[k][0], pts[k][1]);
          }
          ctx.stroke();
        }

        // your GPS position
        if (plugin.gpsIndex >= 0 && !isNaN(e[plugin.gpsIndex])) {
          const gx = xFor(d[plugin.gpsIndex]), gy = yFor(e[plugin.gpsIndex]);
          ctx.fillStyle = Theme.positionColor;
          ctx.strokeStyle = "white";
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(gx, gy, 6, 0, 2 * Math.PI);
          ctx.fill();
          ctx.stroke();
        }

        // scrub cursor
        if (plugin.cursorIndex >= 0) {
          const cx = xFor(d[plugin.cursorIndex]);
          ctx.strokeStyle = "#e8590c";
          ctx.lineWidth = 1.5;
          ctx.beginPath();
          ctx.moveTo(cx, padT);
          ctx.lineTo(cx, padT + plotH);
          ctx.stroke();
          if (!isNaN(e[plugin.cursorIndex])) {
            ctx.fillStyle = "#e8590c";
            ctx.strokeStyle = "white";
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.arc(cx, yFor(e[plugin.cursorIndex]), 5, 0, 2 * Math.PI);
            ctx.fill();
            ctx.stroke();
          }
        }
        ctx.restore();
      }

      // one finger: scrub; two fingers: zoom/pan; double tap: reset
      PinchArea {
        id: pinchArea
        anchors.fill: parent
        property real startSpan: 1
        property real anchorDist: 0
        property real anchorFrac: 0

        onPinchStarted: pinch => {
          startSpan = plugin.viewEnd - plugin.viewStart;
          anchorFrac = Math.max(0, Math.min(1, (pinch.center.x - chart.padL) / chart.plotW));
          anchorDist = plugin.viewStart + anchorFrac * startSpan;
        }
        onPinchUpdated: pinch => {
          const minSpan = Math.min(plugin.totalDist, 150);
          const span = Math.max(minSpan, Math.min(plugin.totalDist, startSpan / pinch.scale));
          const frac = Math.max(0, Math.min(1, (pinch.center.x - chart.padL) / chart.plotW));
          let start = anchorDist - frac * span;
          start = Math.max(0, Math.min(plugin.totalDist - span, start));
          plugin.viewStart = start;
          plugin.viewEnd = start + span;
          chart.requestPaint();
        }

        MouseArea {
          anchors.fill: parent
          preventStealing: true

          function scrub(x) {
            if (plugin.dists.length < 2) {
              return;
            }
            const d = Math.max(plugin.viewStart, Math.min(plugin.viewEnd, chart.distAt(x)));
            plugin.setCursor(plugin.indexAtDistance(d));
          }

          onPressed: mouse => scrub(mouse.x)
          onPositionChanged: mouse => scrub(mouse.x)
          onReleased: plugin.ensureCursorVisible()
          onDoubleClicked: {
            plugin.viewStart = 0;
            plugin.viewEnd = Math.max(1, plugin.totalDist);
            chart.requestPaint();
          }
        }
      }

      // readout bubble for the scrub position
      Rectangle {
        id: bubble
        visible: plugin.cursorIndex >= 0 && plugin.elevs.length === plugin.dists.length
        color: Theme.mainBackgroundColorSemiOpaque
        border.color: "#e8590c"
        border.width: 1
        radius: 6
        width: bubbleText.implicitWidth + 12
        height: bubbleText.implicitHeight + 6
        y: chart.padT + 2
        x: {
          if (plugin.cursorIndex < 0) {
            return 0;
          }
          const cx = chart.xFor(plugin.dists[plugin.cursorIndex]);
          const left = cx + 10 + width > chart.width ? cx - 10 - width : cx + 10;
          return Math.max(chart.padL, left);
        }

        Text {
          id: bubbleText
          anchors.centerIn: parent
          font: Theme.tinyFont
          color: Theme.mainTextColor
          text: {
            const i = plugin.cursorIndex;
            if (i < 0 || i >= plugin.elevs.length) {
              return "";
            }
            const g = plugin.gradeAt(i);
            return plugin.fmtDist(plugin.dists[i]) + "  ·  " + plugin.fmtElev(plugin.elevs[i]) + (isNaN(g) ? "" : "  ·  " + (g >= 0 ? "+" : "") + g.toFixed(0) + "%");
          }
        }
      }

      // status: loading / problems
      Column {
        anchors.centerIn: parent
        width: parent.width - 40
        spacing: 6
        visible: plugin.loading || plugin.message !== ""

        ProgressBar {
          width: parent.width
          visible: plugin.loading
          value: plugin.loadProgress
        }
        Text {
          width: parent.width
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.WordWrap
          font: Theme.tinyFont
          color: Theme.mainTextColor
          text: plugin.loading ? qsTr("Reading elevations from %1…").arg(plugin.demName) : plugin.message
        }
      }
    }
  }

  // ------------------------------------------------------- settings
  Popup {
    id: settingsPopup
    parent: plugin.mainWindow.contentItem
    modal: true
    focus: true
    width: Math.min(parent.width - 32, 380)
    x: (parent.width - width) / 2
    y: (parent.height - height) / 2
    padding: 16

    property var demList: []

    onAboutToShow: {
      const names = [qsTr("Automatic")];
      const candidates = plugin.demCandidates();
      for (let i = 0; i < candidates.length; i++) {
        if (names.indexOf(candidates[i].name) < 0) {
          names.push(candidates[i].name);
        }
      }
      demList = names;
      demBox.currentIndex = Math.max(0, names.indexOf(plugin.preferredDem));
    }

    background: Rectangle {
      color: Theme.mainBackgroundColor
      radius: 12
    }

    Column {
      width: parent.width
      spacing: 10

      Text {
        text: qsTr("Elevation profile settings")
        font: Theme.strongFont
        color: Theme.mainTextColor
      }

      Row {
        spacing: 8
        Text {
          anchors.verticalCenter: parent.verticalCenter
          text: qsTr("Metric units")
          font: Theme.defaultFont
          color: Theme.mainTextColor
        }
        Switch {
          checked: plugin.metric
          onToggled: {
            plugin.metric = checked;
            plugin.saveSettings();
            chart.requestPaint();
          }
        }
      }

      Text {
        width: parent.width
        wrapMode: Text.WordWrap
        text: qsTr("Ignore climbs smaller than %1").arg(plugin.fmtElev(plugin.gainThreshold))
        font: Theme.defaultFont
        color: Theme.mainTextColor
      }
      Slider {
        width: parent.width
        from: 0
        to: 10
        stepSize: 0.5
        value: plugin.gainThreshold
        onMoved: {
          plugin.gainThreshold = value;
          plugin.saveSettings();
          plugin.recomputeStats();
        }
      }

      Text {
        text: qsTr("Elevation layer")
        font: Theme.defaultFont
        color: Theme.mainTextColor
      }
      ComboBox {
        id: demBox
        width: parent.width
        model: settingsPopup.demList
        onActivated: index => {
          plugin.preferredDem = index === 0 ? "" : settingsPopup.demList[index];
          plugin.saveSettings();
          if (panel.visible && plugin.lons.length > 0) {
            plugin.startSampling();
          }
        }
      }

      Text {
        width: parent.width
        wrapMode: Text.WordWrap
        visible: plugin.demName !== ""
        text: qsTr("Currently using: %1").arg(plugin.demName)
        font: Theme.tinyFont
        color: Theme.secondaryTextColor
      }

      Button {
        anchors.right: parent.right
        text: qsTr("Close")
        onClicked: settingsPopup.close()
      }
    }
  }

  // ------------------------------------------------------- lifecycle
  Connections {
    target: iface
    function onLoadProjectTriggered() {
      plugin.closeProfile();
      plugin.demId = "";
      plugin.demName = "";
    }
  }

  Component.onCompleted: {
    loadSettings();
    iface.addItemToPluginsToolbar(profileButton);
  }

  Component.onDestruction: {
    stopPicking();
  }
}
