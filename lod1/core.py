"""LOD1 building heights (BHM) from LiDAR point clouds and footprints.

Ground sources:
    lod1_building_from_pointcloud — classified LAS (ground class 2)
    lod1_building_from_dtm        — any LAS + external DTM raster
    lod1_building_auto_dtm        — any LAS; DTM derived with CSF
Batch over a folder tree: batch_lod1_from_basedir. CLI: python -m lod1 --help
"""
import numpy as np
import laspy
import geopandas as gpd
from pathlib import Path
from tqdm import tqdm
from scipy.spatial import cKDTree
import json
import warnings
warnings.filterwarnings('ignore')


def lod1_building_from_pointcloud(
    input_las,
    input_gpkg,
    output_geojson,
    building_layer=None,
    building_classes=[6],
    ground_classes=[2],
    resolution=1.0,
    nodata_value=-9999,
    fill_gaps=True,
    search_radius=50,
    idw_power=2,
    max_coverage=0.90,
    percentile=95,
    target_epsg=4326
):
    """
    Generates LOD1 building models from a classified LAS point cloud
    and building footprint polygons (GPKG).

    BHM Calculation (per building footprint):
        Step 1 — Build CHM per point:
            For each DSM point (class 6 + 2), find the nearest ground point
            (class 2) using cKDTree, then compute:
                CHM(point) = Z_dsm - Z_nearest_ground

        Step 2 — Aggregate per footprint:
            BHM = percentile(CHM values inside footprint, p)

        - CHM values < 0 are clamped to 0 before percentile calculation.
        - If no DSM or CHM points found inside footprint → nodata_value.

    Parameters:
        input_las       (str)      : Path to input .las/.laz file.
        input_gpkg      (str)      : Path to input GeoPackage (.gpkg) file.
        output_geojson  (str)      : Path to output GeoJSON file.
        building_layer  (str/None) : Layer name in GPKG. None = first layer.
        building_classes(list[int]): LAS classification codes for buildings. Default: [6].
        ground_classes  (list[int]): LAS classification codes for ground/DTM. Default: [2].
        resolution      (float)    : Reserved for raster-based processing. Default: 1.0.
        nodata_value    (float)    : NoData sentinel value. Default: -9999.
        fill_gaps       (bool)     : Reserved for gap-fill compatibility. Default: True.
        search_radius   (float)    : Reserved for IDW compatibility. Default: 50.
        idw_power       (float)    : Reserved for IDW compatibility. Default: 2.
        max_coverage    (float)    : Reserved for IDW compatibility. Default: 0.90.
        percentile      (int)      : Percentile of CHM values per footprint. Default: 95.
        target_epsg     (int)      : Output CRS EPSG code. Default: 4326 (WGS84).

    Returns:
        tuple: (GeoDataFrame, None) on success, (None, error_message) on failure.
    """
    try:
        # ------------------------------------------------------------------ #
        # 1. READ LAS
        # ------------------------------------------------------------------ #
        las = laspy.read(input_las)

        las_epsg = None
        try:
            las_crs = las.header.parse_crs()
            if las_crs:
                las_epsg = las_crs.to_epsg()
        except Exception:
            pass

        dsm_classes = list(set(building_classes + ground_classes))

        # DSM points: class 6 + 2
        mask_dsm = np.isin(las.classification, dsm_classes)
        x_dsm = np.array(las.x[mask_dsm])
        y_dsm = np.array(las.y[mask_dsm])
        z_dsm = np.array(las.z[mask_dsm])

        # DTM points: class 2 only
        mask_dtm = np.isin(las.classification, ground_classes)
        x_dtm = np.array(las.x[mask_dtm])
        y_dtm = np.array(las.y[mask_dtm])
        z_dtm = np.array(las.z[mask_dtm])

        if len(x_dsm) == 0:
            return None, f"No DSM points found for classes {dsm_classes}"
        if len(x_dtm) == 0:
            return None, f"No DTM points found for classes {ground_classes}"

        # ------------------------------------------------------------------ #
        # 2. BUILD CHM PER POINT
        #    For every DSM point, find nearest DTM point (XY only),
        #    then CHM = Z_dsm - Z_nearest_dtm
        # ------------------------------------------------------------------ #
        dtm_xy    = np.column_stack((x_dtm, y_dtm))
        dtm_tree  = cKDTree(dtm_xy)

        dsm_xy            = np.column_stack((x_dsm, y_dsm))
        _, nearest_idx    = dtm_tree.query(dsm_xy, k=1, workers=-1)
        z_ground_for_dsm  = z_dtm[nearest_idx]

        chm_values = np.maximum(0.0, z_dsm - z_ground_for_dsm)  # clamp < 0 to 0

        # ------------------------------------------------------------------ #
        # 3. READ GPKG
        # ------------------------------------------------------------------ #
        if building_layer:
            gdf = gpd.read_file(input_gpkg, layer=building_layer)
        else:
            gdf = gpd.read_file(input_gpkg)

        # ------------------------------------------------------------------ #
        # 4. ALIGN CRS
        # ------------------------------------------------------------------ #
        if las_epsg is not None and gdf.crs is not None:
            if gdf.crs.to_epsg() != las_epsg:
                gdf = gdf.to_crs(epsg=las_epsg)

        # ------------------------------------------------------------------ #
        # 5. EXTRACT CHM POINTS PER FOOTPRINT → BHM = P95(CHM)
        #    Vectorized spatial join (GEOS STRtree via geopandas.sjoin)
        #    instead of a per-polygon Python loop with bbox-mask +
        #    point-in-polygon testing. This turns an O(n_buildings *
        #    n_points) scan into a single indexed spatial join, which is
        #    dramatically faster when there are many buildings and/or
        #    many DSM points.
        # ------------------------------------------------------------------ #
        gdf = gdf.reset_index(drop=True)
        gdf['_bid'] = gdf.index

        points_gdf = gpd.GeoDataFrame(
            {'chm': chm_values},
            geometry=gpd.points_from_xy(x_dsm, y_dsm),
            crs=gdf.crs
        )

        joined = gpd.sjoin(
            points_gdf,
            gdf[['_bid', 'geometry']],
            how='inner',
            predicate='within'
        )

        if len(joined) > 0:
            bhm_map = (
                joined.groupby('_bid')['chm']
                .quantile(percentile / 100.0)
                .round(2)
                .to_dict()
            )
        else:
            bhm_map = {}

        gdf['BHM'] = [bhm_map.get(bid, nodata_value) for bid in gdf['_bid']]
        gdf = gdf.drop(columns='_bid')

        # ------------------------------------------------------------------ #
        # 6. EXPORT TO WGS84 GEOJSON
        # ------------------------------------------------------------------ #
        gdf_out = gdf.to_crs(epsg=target_epsg)
        gdf_out['BHM'] = gdf_out['BHM'].apply(
            lambda v: None if v == nodata_value else round(float(v), 2)
        )
        gdf_out.to_file(output_geojson, driver='GeoJSON')

        return gdf_out, None

    except Exception as e:
        return None, str(e)


# ====================================================================== #
# SHARED HELPERS FOR THE DTM-BASED MODES
# ====================================================================== #
DTM_EXTENSIONS = ('.tif', '.tiff', '.vrt', '.img', '.asc')

# Processing stages per ground mode: (key, label, share of a tile's work).
PIPELINES = {
    'las': [('heights', 'Compute heights', 1.0)],
    'dtm': [('read', 'Read LAS', 0.30), ('heights', 'Heights', 0.60), ('export', 'Export', 0.10)],
    'auto': [('read', 'Read LAS', 0.15), ('noise', 'Remove noise', 0.10),
             ('ground', 'Ground filter', 0.30), ('dtm', 'Build DTM', 0.15),
             ('heights', 'Heights', 0.22), ('export', 'Export', 0.08)],
}


def _emit(stage_callback, key, status, detail=None, frac=None):
    if stage_callback:
        stage_callback(key, status, detail, frac)


class _Grid:
    """Regular north-up grid covering a bounding box (row 0 at the top)."""

    def __init__(self, xmin, ymin, xmax, ymax, cell):
        from rasterio.transform import from_origin
        self.cell = float(cell)
        self.x0, self.y1 = float(xmin), float(ymax)
        self.nx = int(np.floor((xmax - xmin) / cell)) + 1
        self.ny = int(np.floor((ymax - ymin) / cell)) + 1
        self.transform = from_origin(self.x0, self.y1, self.cell, self.cell)

    def flat(self, x, y):
        c = np.clip(((x - self.x0) / self.cell).astype(np.int64), 0, self.nx - 1)
        r = np.clip(((self.y1 - y) / self.cell).astype(np.int64), 0, self.ny - 1)
        return r * self.nx + c

    def centers(self):
        xs = self.x0 + (np.arange(self.nx) + 0.5) * self.cell
        ys = self.y1 - (np.arange(self.ny) + 0.5) * self.cell
        return np.meshgrid(xs, ys)

    def rasterize(self, shapes, dtype='uint8', all_touched=False):
        from rasterio.features import rasterize
        shapes = [s for s in shapes if s[0] is not None and not s[0].is_empty]
        if not shapes:
            return np.zeros((self.ny, self.nx), dtype=dtype)
        return rasterize(shapes, out_shape=(self.ny, self.nx), transform=self.transform,
                         fill=0, dtype=dtype, all_touched=all_touched)


def _grid_min(flat, z, size):
    """Minimum z per grid cell (NaN where a cell has no point)."""
    import pandas as pd
    out = np.full(size, np.nan)
    s = pd.Series(z).groupby(flat).min()
    out[s.index.to_numpy()] = s.to_numpy()
    return out


def _fill_nearest(arr):
    """Replace NaN cells with the value of the nearest valid cell."""
    from scipy import ndimage
    nan = np.isnan(arr)
    if not nan.any() or nan.all():
        return arr
    idx = ndimage.distance_transform_edt(nan, return_distances=False, return_indices=True)
    return arr[tuple(idx)]


def _read_points(input_las, exclude_classes=(7, 18), stage_callback=None, chunk=2_000_000):
    """Read XYZ, classification and a 'vegetation-like' flag chunk by chunk
    (keeps memory near 30 bytes per point instead of the whole LAS record).

    veg = class 5 (high vegetation) or a non-last return of a multi-return
    pulse: canopy splits the beam, solid roofs usually do not."""
    with laspy.open(input_las) as f:
        h = f.header
        n = h.point_count
        crs = None
        try:
            crs = h.parse_crs()
        except Exception:
            pass
        x = np.empty(n)
        y = np.empty(n)
        z = np.empty(n)
        veg = np.zeros(n, dtype=bool)
        keep = np.ones(n, dtype=bool)
        i = 0
        for c in f.chunk_iterator(chunk):
            m = len(c)
            cls = np.asarray(c.classification)
            x[i:i + m] = c.x
            y[i:i + m] = c.y
            z[i:i + m] = c.z
            rn = np.asarray(c.return_number)
            nr = np.asarray(c.number_of_returns)
            veg[i:i + m] = (cls == 5) | ((nr > 1) & (rn < nr))
            keep[i:i + m] = ~np.isin(cls, list(exclude_classes))
            i += m
            _emit(stage_callback, 'read', 'running', f"{i / 1e6:.1f} / {n / 1e6:.1f} M points", i / max(n, 1))
        bounds = (h.mins[0], h.mins[1], h.maxs[0], h.maxs[1])
    keep[i:] = False
    return {'x': x[keep], 'y': y[keep], 'z': z[keep], 'veg': veg[keep],
            'crs': crs, 'bounds': bounds, 'n_total': n}


def _read_footprints(input_gpkg, building_layer, las_crs):
    gdf = gpd.read_file(input_gpkg, layer=building_layer) if building_layer else gpd.read_file(input_gpkg)
    if las_crs is not None and gdf.crs is not None and gdf.crs != las_crs:
        gdf = gdf.to_crs(las_crs)
    return gdf.reset_index(drop=True)


def _read_dtm(dtm_path, bounds, bounds_crs):
    """Read the DTM covering ``bounds`` (xmin, ymin, xmax, ymax in
    ``bounds_crs``). ``dtm_path`` is a raster file or a folder of tiles; tiles
    intersecting the bounds are mosaicked.

    Returns (array float64 with NaN as nodata, affine transform, dtm CRS)."""
    import rasterio
    from rasterio.merge import merge
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    dtm_path = Path(dtm_path)
    if dtm_path.is_dir():
        paths = sorted(p for p in dtm_path.rglob('*') if p.suffix.lower() in DTM_EXTENSIONS)
    else:
        paths = [dtm_path]
    if not paths:
        raise ValueError(f"No DTM raster found in {dtm_path}")

    hits = []
    for p in paths:
        src = rasterio.open(p)
        b = bounds
        if bounds_crs is not None and src.crs is not None and src.crs != bounds_crs:
            b = transform_bounds(bounds_crs, src.crs, *bounds, densify_pts=21)
        # Pad by 2 pixels so bilinear sampling at the edges has neighbours.
        pad = 2 * max(abs(src.res[0]), abs(src.res[1]))
        b = (b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad)
        sb = src.bounds
        if b[0] < sb.right and b[2] > sb.left and b[1] < sb.top and b[3] > sb.bottom:
            hits.append((src, b))
        else:
            src.close()

    if not hits:
        raise ValueError("DTM does not cover this tile")

    try:
        src, b = hits[0]
        nodata = src.nodata
        crs = src.crs
        if len(hits) == 1:
            win = from_bounds(*b, transform=src.transform).round_offsets().round_lengths()
            arr = src.read(1, window=win, boundless=True, masked=True)
            transform = src.window_transform(win)
        else:
            union = (min(h[1][0] for h in hits), min(h[1][1] for h in hits),
                     max(h[1][2] for h in hits), max(h[1][3] for h in hits))
            data, transform = merge([h[0] for h in hits], bounds=union, masked=True)
            arr = data[0]
        arr = np.ma.filled(np.ma.asarray(arr).astype('float64'), np.nan)
        if nodata is not None and not np.isnan(nodata):
            arr[arr == nodata] = np.nan
    finally:
        for h in hits:
            h[0].close()
    return arr, transform, crs


def _sample_bilinear(arr, transform, xs, ys):
    """Bilinear DTM value at (xs, ys); falls back to the nearest pixel where a
    neighbour is NoData. Points outside the raster get NaN."""
    h, w = arr.shape
    cols, rows = ~transform * (xs, ys)
    out = np.full(len(xs), np.nan)
    inside = (cols >= 0) & (cols < w) & (rows >= 0) & (rows < h)
    if not inside.any():
        return out
    c, r = cols[inside], rows[inside]

    # Bilinear on pixel centres (+0.5), clamped at the raster border.
    cc = np.clip(c - 0.5, 0, max(w - 1, 0))
    rr = np.clip(r - 0.5, 0, max(h - 1, 0))
    c0 = np.minimum(np.floor(cc).astype(int), max(w - 2, 0))
    r0 = np.minimum(np.floor(rr).astype(int), max(h - 2, 0))
    c1, r1 = np.minimum(c0 + 1, w - 1), np.minimum(r0 + 1, h - 1)
    dc, dr = cc - c0, rr - r0
    val = (arr[r0, c0] * (1 - dc) * (1 - dr) + arr[r0, c1] * dc * (1 - dr)
           + arr[r1, c0] * (1 - dc) * dr + arr[r1, c1] * dc * dr)

    nearest = arr[np.floor(r).astype(int), np.floor(c).astype(int)]
    val = np.where(np.isnan(val), nearest, val)
    out[inside] = val
    return out


def _estimate_dtm_offset(x, y, z, footprints, ground_fn, cell=10.0):
    """Median of (lowest LAS point per cell - ground_fn) over cells outside the
    footprints, i.e. mostly bare ground. For an external DTM this is the
    vertical offset the DTM needs; for a DTM built from the LAS it should be
    close to 0. Returns None when there are too few such cells."""
    import pandas as pd
    if len(x) > 2_000_000:
        idx = np.random.default_rng(0).choice(len(x), 2_000_000, replace=False)
        x, y, z = x[idx], y[idx], z[idx]
    df = pd.DataFrame({'x': x, 'y': y, 'z': z,
                       'cx': np.floor(x / cell).astype(np.int64),
                       'cy': np.floor(y / cell).astype(np.int64)})
    low = df.loc[df.groupby(['cx', 'cy'])['z'].idxmin()]
    pts = gpd.GeoSeries(gpd.points_from_xy(low['x'], low['y']), crs=footprints.crs)
    outside = ~pts.intersects(footprints.union_all()).to_numpy()
    low = low[outside]
    if len(low) < 10:
        return None
    diff = low['z'].to_numpy() - ground_fn(low['x'].to_numpy(), low['y'].to_numpy())
    diff = diff[~np.isnan(diff)]
    return float(np.median(diff)) if len(diff) >= 10 else None


def _footprint_heights(gdf, x, y, z, veg, ground_fn, data_bounds, percentile=95,
                       min_height=0.0, inner_buffer=0.0, min_points=1,
                       small_area=20.0, stage_callback=None):
    """Height and QC per footprint.

    - Footprints larger than ``small_area`` m² are shrunk by ``inner_buffer``
      so wall and eave points stay out; small ones keep their outline.
    - HAG = z - ground_fn(x, y). Points flagged ``veg`` and points with
      HAG < ``min_height`` are ignored; BHM = percentile of the rest.

    Returns dict of arrays (one entry per footprint): bhm (NaN = none),
    n_points, qc_flag, plus n_missing (points with no ground value)."""
    import pandas as pd
    import shapely
    from shapely.geometry import box

    n = len(gdf)
    shrunk = []
    for g in gdf.geometry.values:
        s = g
        if g is not None and inner_buffer > 0 and g.area > small_area:
            s = g.buffer(-inner_buffer)
            if s.is_empty or s.area < 0.3 * g.area:
                s = g
        shrunk.append(s)

    bhm = np.full(n, np.nan)
    n_points = np.zeros(n, dtype=np.int64)
    roof_share = np.full(n, np.nan)
    veg_share = np.zeros(n)

    valid_geoms = [s for s in shrunk if s is not None and not s.is_empty]
    n_missing = 0
    if valid_geoms:
        xmin, ymin, xmax, ymax = gpd.GeoSeries(valid_geoms).total_bounds
        grid = _Grid(xmin, ymin, xmax, ymax, 0.5)
        labels = grid.rasterize([(s, i + 1) for i, s in enumerate(shrunk)],
                                dtype='int32', all_touched=True).ravel()
        cand = np.flatnonzero((x >= xmin) & (x <= xmax) & (y >= ymin) & (y <= ymax))
        lab = labels[grid.flat(x[cand], y[cand])]
        cand, lab = cand[lab > 0], lab[lab > 0] - 1

        # Exact point-in-polygon, one footprint at a time on its raster candidates.
        order = np.argsort(lab, kind='stable')
        cand, lab = cand[order], lab[order]
        starts = np.searchsorted(lab, np.arange(n))
        ends = np.searchsorted(lab, np.arange(n), side='right')
        inside = np.zeros(len(cand), dtype=bool)
        for i in range(n):
            a, b = starts[i], ends[i]
            if b > a:
                inside[a:b] = shapely.contains_xy(shrunk[i], x[cand[a:b]], y[cand[a:b]])
            if i % 200 == 0:
                _emit(stage_callback, 'heights', 'running', f"{i} / {n} footprints", 0.6 * i / max(n, 1))
        cand, lab = cand[inside], lab[inside]

        hag = z[cand] - ground_fn(x[cand], y[cand])
        ok = ~np.isnan(hag)
        n_missing = int((~ok).sum())
        is_veg = veg[cand] if veg is not None else np.zeros(len(cand), dtype=bool)
        df = pd.DataFrame({'fid': lab, 'hag': hag, 'veg': is_veg, 'ok': ok})
        df = df[df['ok']]
        if len(df):
            veg_share_s = df.groupby('fid')['veg'].mean()
            veg_share[veg_share_s.index] = veg_share_s.to_numpy()
            solid = df[~df['veg']]
            use = solid[solid['hag'] >= min_height]
            cnt_solid = solid.groupby('fid').size()
            cnt_use = use.groupby('fid').size()
            n_points[cnt_use.index] = cnt_use.to_numpy()
            share = (cnt_use.reindex(cnt_solid.index, fill_value=0) / cnt_solid)
            roof_share[share.index] = share.to_numpy()
            if len(use):
                q = use.groupby('fid')['hag'].quantile(percentile / 100.0)
                bhm[q.index] = q.round(2).to_numpy()
        _emit(stage_callback, 'heights', 'running', f"{n} / {n} footprints", 0.9)

    data_box = box(*data_bounds).buffer(1e-6)
    flags = []
    for i, g in enumerate(gdf.geometry.values):
        f = []
        if g is None or not data_box.contains(g):
            f.append('edge_of_data')
        if n_points[i] < min_points:
            f.append('few_points')
        if not np.isnan(roof_share[i]) and roof_share[i] < 0.5:
            f.append('low_structure')
        if veg_share[i] > 0.3:
            f.append('vegetation_cover')
        flags.append(';'.join(f) or 'ok')
    return {'bhm': bhm, 'n_points': n_points, 'qc_flag': flags, 'n_missing': n_missing}


def _write_output(gdf, heights, output_geojson, target_epsg, attrs):
    gdf = gdf.copy()
    gdf['BHM'] = [None if np.isnan(v) else round(float(v), 2) for v in heights['bhm']]
    gdf['n_points'] = heights['n_points']
    gdf['qc_flag'] = heights['qc_flag']
    gdf_out = gdf.to_crs(epsg=target_epsg) if gdf.crs is not None else gdf
    gdf_out.to_file(output_geojson, driver='GeoJSON')
    gdf_out.attrs.update(attrs)
    return gdf_out


# ====================================================================== #
# EXTERNAL DTM (for unclassified point clouds)
# ====================================================================== #
def lod1_building_from_dtm(
    input_las,
    input_gpkg,
    output_geojson,
    dtm_path,
    building_layer=None,
    dtm_offset=0.0,
    exclude_classes=(7, 18),
    percentile=95,
    nodata_value=-9999,
    target_epsg=4326,
    min_height=0.0,
    inner_buffer=0.0,
    min_points=1,
    stage_callback=None
):
    """
    LOD1 building heights from an UNCLASSIFIED point cloud plus an external
    DTM (GeoTIFF/VRT file, or a folder of DTM tiles).

    BHM Calculation (per building footprint):
        Step 1 — Height above ground per point:
            For every LAS point inside a footprint, sample the DTM at its XY
            (bilinear) and compute:
                HAG(point) = Z_las - (Z_dtm + dtm_offset)
        Step 2 — Aggregate per footprint:
            BHM = percentile(HAG of non-vegetation points with
                             HAG >= min_height, p)

        - No LAS classification is needed; only noise classes in
          ``exclude_classes`` (default 7 and 18) are dropped. Class 5 and
          non-last returns are treated as vegetation.
        - ``dtm_offset`` reconciles vertical datums. Example: LAS heights are
          ellipsoidal and the DTM is orthometric (EGM2008, e.g. DEMNAS)
          → dtm_offset = geoid undulation N at the site.
        - The lowest LAS point per 10 m cell outside the footprints (bare
          ground) is compared with the DTM; if it is more than 1 m off a
          WARNING line is printed with a suggested offset.

    Output columns: BHM, n_points, qc_flag (see _footprint_heights).

    Returns:
        tuple: (GeoDataFrame, None) on success, (None, error_message) on failure.
    """
    try:
        name = Path(input_las).name

        _emit(stage_callback, 'read', 'running')
        pts = _read_points(input_las, exclude_classes, stage_callback)
        x, y, z = pts['x'], pts['y'], pts['z']
        if len(x) == 0:
            return None, "No usable points in LAS (all points are noise classes)"
        gdf = _read_footprints(input_gpkg, building_layer, pts['crs'])
        pts_crs = pts['crs'] if pts['crs'] is not None else gdf.crs
        _emit(stage_callback, 'read', 'done', f"{len(x) / 1e6:.1f} M points")

        _emit(stage_callback, 'heights', 'running', 'reading DTM')
        xmin, ymin, xmax, ymax = gdf.total_bounds
        dtm, transform, dtm_crs = _read_dtm(
            dtm_path, (max(xmin, pts['bounds'][0]), max(ymin, pts['bounds'][1]),
                       min(xmax, pts['bounds'][2]), min(ymax, pts['bounds'][3])), pts_crs)
        if pts_crs is not None and dtm_crs is not None and pts_crs != dtm_crs:
            from pyproj import Transformer
            to_dtm = Transformer.from_crs(pts_crs, dtm_crs, always_xy=True).transform
        else:
            to_dtm = lambda a, b: (a, b)

        def ground_raw(xx, yy):
            return _sample_bilinear(dtm, transform, *to_dtm(xx, yy))

        heights = _footprint_heights(
            gdf, x, y, z, pts['veg'], lambda xx, yy: ground_raw(xx, yy) + dtm_offset,
            pts['bounds'], percentile, min_height, inner_buffer, min_points,
            stage_callback=stage_callback)
        if heights['n_missing']:
            print(f"WARNING: {name}: {heights['n_missing']} points have no DTM "
                  f"value (outside DTM or NoData) and were ignored.")

        # Bare ground outside the footprints should sit on DTM + offset.
        # A plausible-looking BHM can still hide e.g. a +30 m datum shift.
        est = _estimate_dtm_offset(x, y, z, gdf, ground_raw)
        if est is not None and abs(est - dtm_offset) > 1.0:
            print(f"WARNING: {name}: bare ground is {est - dtm_offset:+.2f} m away from "
                  f"DTM + offset ({dtm_offset:.2f} m). Check the vertical datum; "
                  f"suggested offset: {est:.2f} m.")
        _emit(stage_callback, 'heights', 'done', f"{int(np.sum(~np.isnan(heights['bhm'])))} with height")

        _emit(stage_callback, 'export', 'running')
        gdf_out = _write_output(gdf, heights, output_geojson, target_epsg, {
            'ground_offset': None if est is None else est - dtm_offset, 'dtm_path': None})
        _emit(stage_callback, 'export', 'done')
        return gdf_out, None

    except Exception as e:
        return None, str(e)


# ====================================================================== #
# AUTO DTM (ground filtered from the LAS itself)
# ====================================================================== #
def lod1_building_auto_dtm(
    input_las,
    input_gpkg,
    output_geojson,
    output_dtm=None,
    building_layer=None,
    exclude_classes=(7, 18),
    percentile=95,
    min_height=2.0,
    inner_buffer=0.5,
    min_points=10,
    dtm_res=0.5,
    rigidness=2,
    max_height=100.0,
    target_epsg=4326,
    stage_callback=None
):
    """
    LOD1 building heights from an UNCLASSIFIED point cloud alone: the ground
    model is derived from the same LAS, so no external DTM or datum offset is
    needed.

    Pipeline:
        1. Read      — XYZ + vegetation flag (class 5 or non-last return).
        2. Noise     — per 1 m cell lowest point; reference = 30th percentile
                       of those minima in a 5×5 window (footprint cells left
                       out). Points > 2 m below it or > ``max_height`` above
                       it are dropped.
        3. Ground    — candidates exclude footprints (+1 m), vegetation and
                       noise; thinned to the lowest point per ``dtm_res``
                       cell; Cloth Simulation Filter (CSF) keeps the ground.
        4. DTM       — TIN (linear Delaunay) of ground points on a
                       ``dtm_res`` grid, so ground under buildings is
                       interpolated from around them; written to
                       ``output_dtm`` (GeoTIFF) when given.
        5. Heights   — as lod1_building_from_dtm: BHM = P(percentile) of HAG
                       of non-vegetation points with HAG >= ``min_height``
                       inside footprints shrunk by ``inner_buffer``.

    ``rigidness``: CSF cloth stiffness — 1 steep, 2 hilly, 3 flat terrain.

    Returns:
        tuple: (GeoDataFrame, None) on success, (None, error_message) on failure.
    """
    try:
        import pandas as pd
        from scipy import ndimage
        from scipy.interpolate import LinearNDInterpolator
        name = Path(input_las).name

        # 1. READ
        _emit(stage_callback, 'read', 'running')
        pts = _read_points(input_las, exclude_classes, stage_callback)
        x, y, z, veg = pts['x'], pts['y'], pts['z'], pts['veg']
        if len(x) == 0:
            return None, "No usable points in LAS"
        gdf = _read_footprints(input_gpkg, building_layer, pts['crs'])
        _emit(stage_callback, 'read', 'done', f"{len(x) / 1e6:.1f} M points · {len(gdf)} footprints")

        # 2. NOISE
        _emit(stage_callback, 'noise', 'running')
        xmin, ymin, xmax, ymax = x.min(), y.min(), x.max(), y.max()
        g1 = _Grid(xmin, ymin, xmax, ymax, 1.0)
        f1 = g1.flat(x, y)
        cmin = _grid_min(f1, z, g1.nx * g1.ny).reshape(g1.ny, g1.nx)
        fp_mask = g1.rasterize([(g.buffer(1.0), 1) for g in gdf.geometry.values if g is not None]).astype(bool)
        cmin[fp_mask] = np.nan
        _emit(stage_callback, 'noise', 'running', 'reference surface', 0.5)
        ref = ndimage.percentile_filter(_fill_nearest(cmin), 30, size=5).ravel()
        rz = ref[f1]
        low = z < rz - 2.0
        high = z > rz + max_height
        noise = low | high
        del f1, rz
        _emit(stage_callback, 'noise', 'done', f"{int(low.sum()):,} low · {int(high.sum()):,} high removed")

        # 3. GROUND FILTER (CSF)
        _emit(stage_callback, 'ground', 'running', 'selecting candidates')
        gres = _Grid(xmin, ymin, xmax, ymax, 0.5)
        fp_buf = gres.rasterize([(g.buffer(1.0), 1) for g in gdf.geometry.values if g is not None]).ravel().astype(bool)
        cand = np.flatnonzero(~noise & ~veg)
        cand = cand[~fp_buf[gres.flat(x[cand], y[cand])]]
        gd = _Grid(xmin, ymin, xmax, ymax, dtm_res)
        cell = gd.flat(x[cand], y[cand])
        lowest = pd.Series(z[cand]).groupby(cell).idxmin().to_numpy()
        cand = cand[lowest]
        _emit(stage_callback, 'ground', 'running', f"CSF on {len(cand):,} points", 0.1)

        import CSF
        csf = CSF.CSF()
        csf.params.bSloopSmooth = True
        csf.params.cloth_resolution = max(float(dtm_res), 0.5)
        csf.params.rigidness = int(rigidness)
        csf.params.class_threshold = 0.5
        csf.params.interations = 500
        csf.params.time_step = 0.65
        ox, oy = xmin, ymin
        csf.setPointCloud(np.column_stack([x[cand] - ox, y[cand] - oy, z[cand]]))
        g_idx, ng_idx = CSF.VecInt(), CSF.VecInt()
        csf.do_filtering(g_idx, ng_idx, exportCloth=False)
        ground = cand[np.asarray(g_idx, dtype=np.int64)]
        if len(ground) < 3:
            return None, "Ground filter found no ground points"
        _emit(stage_callback, 'ground', 'done', f"{len(ground):,} ground points")

        # 4. DTM (TIN → raster)
        _emit(stage_callback, 'dtm', 'running', f"TIN of {len(ground):,} points")
        tin = LinearNDInterpolator(np.column_stack([x[ground] - ox, y[ground] - oy]), z[ground])
        cx, cy = gd.centers()
        dtm = tin(cx - ox, cy - oy)
        dtm = _fill_nearest(dtm)
        if output_dtm:
            import rasterio
            # Cells more than ~3 m from any LAS point are outside the survey:
            # NoData in the file (sampling still uses the filled surface).
            covered = np.zeros(gd.nx * gd.ny, dtype=bool)
            covered[gd.flat(x, y)] = True
            covered = ndimage.binary_dilation(covered.reshape(gd.ny, gd.nx),
                                              iterations=max(1, int(round(3 / dtm_res))))
            Path(output_dtm).parent.mkdir(parents=True, exist_ok=True)
            with rasterio.open(output_dtm, 'w', driver='GTiff', width=gd.nx, height=gd.ny, count=1,
                               dtype='float32', crs=pts['crs'] or gdf.crs, transform=gd.transform,
                               nodata=-9999, compress='deflate') as dst:
                dst.write(np.where(covered, dtm, -9999).astype('float32'), 1)
        _emit(stage_callback, 'dtm', 'done', f"{gd.nx}×{gd.ny} @ {dtm_res} m")

        # 5. HEIGHTS
        _emit(stage_callback, 'heights', 'running')

        def ground_fn(xx, yy):
            return _sample_bilinear(dtm, gd.transform, xx, yy)

        keep = ~noise
        heights = _footprint_heights(
            gdf, x[keep], y[keep], z[keep], veg[keep], ground_fn, pts['bounds'],
            percentile, min_height, inner_buffer, min_points, stage_callback=stage_callback)
        fit = _estimate_dtm_offset(x[keep], y[keep], z[keep], gdf, ground_fn)
        if fit is not None and abs(fit) > 0.5:
            print(f"WARNING: {name}: bare ground sits {fit:+.2f} m from the derived DTM; "
                  f"check the ground filter (terrain setting).")
        _emit(stage_callback, 'heights', 'done', f"{int(np.sum(~np.isnan(heights['bhm'])))} with height")

        # 6. EXPORT
        _emit(stage_callback, 'export', 'running')
        gdf_out = _write_output(gdf, heights, output_geojson, target_epsg, {
            'ground_offset': fit, 'dtm_path': str(output_dtm) if output_dtm else None})
        _emit(stage_callback, 'export', 'done')
        return gdf_out, None

    except Exception as e:
        return None, str(e)


def batch_lod1(
    process_json,
    las_dir,
    gpkg_dir,
    output_dir,
    building_layer=None,
    building_classes=[6],
    ground_classes=[2],
    resolution=1.0,
    nodata_value=-9999,
    fill_gaps=True,
    search_radius=50,
    idw_power=2,
    max_coverage=0.90,
    percentile=95,
    target_epsg=4326,
    las_extensions=('.las', '.laz')
):
    """
    Batch processes LOD1 building generation for tiles listed in a JSON file.

    JSON format expected:
        { "process": ["50000_10000", "5000_1000002", ...] }

    Matching logic:
    - Reads tile names from the 'process' key in the JSON file.
    - For each name, searches `las_dir` for a matching LAS/LAZ file
      and `gpkg_dir` for a matching GPKG file (case-insensitive stem match).
    - Reports missing LAS or GPKG files in the summary as 'skipped'.

    Parameters:
        process_json    (str)      : Path to JSON file containing tile names.
        las_dir         (str)      : Directory containing .las/.laz files.
        gpkg_dir        (str)      : Directory containing .gpkg files.
        output_dir      (str)      : Directory to save output .geojson files.
        building_layer  (str/None) : GPKG layer name. None = first layer.
        building_classes(list[int]): LAS classification codes. Default: [6].
        ground_classes  (list[int]): LAS classification codes for ground. Default: [2].
        resolution      (float)    : Reserved for raster-based processing. Default: 1.0.
        nodata_value    (float)    : NoData sentinel. Default: -9999.
        fill_gaps       (bool)     : Reserved for gap-fill compatibility. Default: True.
        search_radius   (float)    : Reserved for IDW compatibility. Default: 50.
        idw_power       (float)    : Reserved for IDW compatibility. Default: 2.
        max_coverage    (float)    : Max fraction of raster to fill. Default: 0.90.
        percentile      (int)      : Percentile of CHM per footprint. Default: 95.
        target_epsg     (int)      : Output CRS EPSG. Default: 4326.
        las_extensions  (tuple)    : LAS file extensions to match. Default: ('.las', '.laz').

    Returns:
        dict: Summary with keys 'success', 'skipped', 'failed'.
    """
    las_dir    = Path(las_dir)
    gpkg_dir   = Path(gpkg_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(process_json, 'r') as f:
        config = json.load(f)

    tile_names = config.get('process', [])

    if not tile_names:
        print("No tile names found under 'process' key in JSON.")
        return {'success': [], 'skipped': [], 'failed': []}

    las_lookup = {
        f.stem.lower(): f for f in las_dir.iterdir()
        if f.suffix.lower() in las_extensions
    }
    gpkg_lookup = {
        f.stem.lower(): f for f in gpkg_dir.iterdir()
        if f.suffix.lower() == '.gpkg'
    }

    matched = []
    skipped = []

    for name in tile_names:
        key       = name.lower()
        las_path  = las_lookup.get(key)
        gpkg_path = gpkg_lookup.get(key)

        if las_path is None and gpkg_path is None:
            skipped.append({'name': name, 'reason': 'LAS and GPKG not found'})
        elif las_path is None:
            skipped.append({'name': name, 'reason': 'LAS not found'})
        elif gpkg_path is None:
            skipped.append({'name': name, 'reason': 'GPKG not found'})
        else:
            matched.append((name, las_path, gpkg_path))

    summary = {'success': [], 'skipped': skipped, 'failed': []}

    if not matched:
        print("No valid LAS–GPKG pairs resolved from JSON.")
        return summary

    print(f"\nBatch LOD1 | {len(matched)} matched | {len(skipped)} skipped\n")

    with tqdm(total=len(matched), unit='tile', ncols=72, colour='green') as pbar:
        for name, las_path, gpkg_path in matched:
            pbar.set_description(f"{name[:30]:<30}")

            output_path = output_dir / f"{name}_lod1.geojson"

            result, error = lod1_building_from_pointcloud(
                input_las        = str(las_path),
                input_gpkg       = str(gpkg_path),
                output_geojson   = str(output_path),
                building_layer   = building_layer,
                building_classes = building_classes,
                ground_classes   = ground_classes,
                resolution       = resolution,
                nodata_value     = nodata_value,
                fill_gaps        = fill_gaps,
                search_radius    = search_radius,
                idw_power        = idw_power,
                max_coverage     = max_coverage,
                percentile       = percentile,
                target_epsg      = target_epsg
            )

            if result is not None:
                summary['success'].append(name)
            else:
                summary['failed'].append({'name': name, 'reason': error})

            pbar.update(1)

    total   = len(matched)
    success = len(summary['success'])
    failed  = len(summary['failed'])
    n_skip  = len(summary['skipped'])

    print(f"\n{'='*45}")
    print(f"  Batch Complete")
    print(f"{'='*45}")
    print(f"  ✅ Success  : {success}/{total}")
    print(f"  ❌ Failed   : {failed}/{total}")
    print(f"  ⏭  Skipped  : {n_skip} (from JSON list)")

    if summary['failed']:
        print(f"\n  Failed tiles:")
        for item in summary['failed']:
            print(f"    - {item['name']}: {item['reason']}")

    if summary['skipped']:
        print(f"\n  Skipped tiles:")
        for item in summary['skipped']:
            print(f"    - {item['name']}: {item['reason']}")

    print(f"{'='*45}\n")

    return summary


def find_tile_pairs(basedir, las_extensions=('.las', '.laz')):
    """Pair every LAS/LAZ under ``basedir`` with the GPKG in its folder.

    Returns (matched, skipped): matched = [(label, las_path, gpkg_path,
    relative_folder)], skipped = [{'name', 'reason'}]. Any GPKG name works;
    with several GPKG in a folder only one named like the LAS is accepted."""
    basedir = Path(basedir)
    # rglob also supports nested folder structures below basedir.
    las_files = sorted(p for p in basedir.rglob('*') if p.is_file()
                       and p.suffix.lower() in las_extensions)
    matched = []
    skipped = []
    for las_path in las_files:
        relative_folder = las_path.parent.relative_to(basedir)
        label = str(relative_folder / las_path.stem)
        gpkgs = sorted(p for p in las_path.parent.iterdir()
                       if p.is_file() and p.suffix.lower() == '.gpkg')

        if len(gpkgs) > 1:
            # Several candidates: only a GPKG named like the LAS is unambiguous.
            gpkgs = [p for p in gpkgs if p.stem.lower() == las_path.stem.lower()] or gpkgs

        if not gpkgs:
            skipped.append({'name': label, 'reason': 'No GPKG found in the same folder'})
        elif len(gpkgs) > 1:
            names = ', '.join(p.name for p in gpkgs)
            skipped.append({'name': label, 'reason': f'Multiple GPKG found, cannot choose: {names}'})
        else:
            matched.append((label, las_path, gpkgs[0], relative_folder))
    return matched, skipped


def batch_lod1_from_basedir(
    basedir,
    output_dir,
    building_layer=None,
    building_classes=[6],
    ground_classes=[2],
    resolution=1.0,
    nodata_value=-9999,
    fill_gaps=True,
    search_radius=50,
    idw_power=2,
    max_coverage=0.90,
    percentile=95,
    target_epsg=4326,
    las_extensions=('.las', '.laz'),
    progress_callback=None,
    stop_event=None,
    dtm_path=None,
    dtm_offset=0.0,
    ground_mode=None,
    min_height=2.0,
    inner_buffer=0.5,
    min_points=10,
    dtm_res=0.5,
    rigidness=2,
    stage_callback=None,
    tile_callback=None
):
    """Process LAS/LAZ and GPKG pairs found in every subfolder of ``basedir``.

    ground_mode:
        'las'  — LAS ground class 2 (lod1_building_from_pointcloud).
        'dtm'  — external DTM ``dtm_path`` + ``dtm_offset``; LAS may be
                 unclassified (lod1_building_from_dtm).
        'auto' — DTM derived from the LAS itself with CSF; also writes
                 ``<stem>_dtm.tif`` (lod1_building_auto_dtm).
        None   — 'dtm' when ``dtm_path`` is given, else 'las'.
    min_height / inner_buffer / min_points apply to 'dtm' and 'auto';
    dtm_res / rigidness to 'auto' only.

    Every LAS/LAZ file uses the GPKG located in the same folder as its
    building-footprint input, whatever the GPKG is named. If a folder holds
    several GPKG files, the one with the same name as the LAS is used;
    otherwise the LAS is skipped as ambiguous. Output files retain the
    input subfolder structure under ``output_dir`` to avoid name collisions.

    progress_callback(done_idx, total, label, done_bytes, total_bytes) is
    called before each tile starts and once more when all tiles finish.
    stage_callback(tile_idx, stage_key, status, detail, frac) reports the
    stages in PIPELINES[ground_mode] within a tile.
    tile_callback(tile_idx, label, stats) runs after each tile (stats also
    collected in summary['tiles'][label]).
    stop_event (threading.Event) cancels the batch between tiles when set.
    """
    basedir = Path(basedir)
    output_dir = Path(output_dir)

    if not basedir.is_dir():
        raise ValueError(f"Base directory does not exist or is not a directory: {basedir}")
    if ground_mode is None:
        ground_mode = 'dtm' if dtm_path else 'las'

    matched, skipped = find_tile_pairs(basedir, las_extensions)
    summary = {'success': [], 'skipped': skipped, 'failed': [], 'tiles': {}}
    if not matched:
        print('No LAS/LAZ–GPKG pairs found under the base directory.')
        return summary

    print(f"\nBatch LOD1 | {len(matched)} matched | {len(skipped)} skipped\n")
    total_bytes = sum(m[1].stat().st_size for m in matched)
    done_bytes = 0
    with tqdm(total=len(matched), unit='tile', ncols=72, colour='green',
              disable=progress_callback is not None) as pbar:
        for i, (label, las_path, gpkg_path, relative_folder) in enumerate(matched):
            if stop_event is not None and stop_event.is_set():
                summary['cancelled'] = True
                break
            if progress_callback:
                progress_callback(i, len(matched), label, done_bytes, total_bytes)
            pbar.set_description(f"{label[:30]:<30}")
            output_path = output_dir / relative_folder / f"{las_path.stem}_lod1.geojson"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            on_stage = (lambda *a, _i=i: stage_callback(_i, *a)) if stage_callback else None

            if ground_mode == 'auto':
                result, error = lod1_building_auto_dtm(
                    input_las=str(las_path),
                    input_gpkg=str(gpkg_path),
                    output_geojson=str(output_path),
                    output_dtm=str(output_dir / relative_folder / f"{las_path.stem}_dtm.tif"),
                    building_layer=building_layer,
                    percentile=percentile,
                    min_height=min_height,
                    inner_buffer=inner_buffer,
                    min_points=min_points,
                    dtm_res=dtm_res,
                    rigidness=rigidness,
                    target_epsg=target_epsg,
                    stage_callback=on_stage,
                )
            elif ground_mode == 'dtm':
                result, error = lod1_building_from_dtm(
                    input_las=str(las_path),
                    input_gpkg=str(gpkg_path),
                    output_geojson=str(output_path),
                    dtm_path=dtm_path,
                    building_layer=building_layer,
                    dtm_offset=dtm_offset,
                    percentile=percentile,
                    nodata_value=nodata_value,
                    target_epsg=target_epsg,
                    min_height=min_height,
                    inner_buffer=inner_buffer,
                    min_points=min_points,
                    stage_callback=on_stage,
                )
            else:
                _emit(on_stage, 'heights', 'running')
                result, error = lod1_building_from_pointcloud(
                    input_las=str(las_path),
                    input_gpkg=str(gpkg_path),
                    output_geojson=str(output_path),
                    building_layer=building_layer,
                    building_classes=building_classes,
                    ground_classes=ground_classes,
                    resolution=resolution,
                    nodata_value=nodata_value,
                    fill_gaps=fill_gaps,
                    search_radius=search_radius,
                    idw_power=idw_power,
                    max_coverage=max_coverage,
                    percentile=percentile,
                    target_epsg=target_epsg,
                )

            if ground_mode == 'las':
                _emit(on_stage, 'heights', 'done' if result is not None else 'failed')

            if result is not None:
                summary['success'].append(label)
                attrs = result.attrs
                stats = {
                    'output': str(output_path),
                    'dtm': attrs.get('dtm_path'),
                    'ground_offset': attrs.get('ground_offset'),
                    'bhm': [None if v is None or v != v else float(v) for v in result['BHM']],
                    'n_points': ([int(v) for v in result['n_points']]
                                 if 'n_points' in result else None),
                    'qc_flag': list(result['qc_flag']) if 'qc_flag' in result else None,
                }
            else:
                summary['failed'].append({'name': label, 'reason': error})
                stats = {'error': error}
            summary['tiles'][label] = stats
            if tile_callback:
                tile_callback(i, label, stats)
            done_bytes += las_path.stat().st_size
            pbar.update(1)

    if progress_callback and not summary.get('cancelled'):
        progress_callback(len(matched), len(matched), '', done_bytes, total_bytes)

    print(f"\nCompleted: {len(summary['success'])} success, "
          f"{len(summary['failed'])} failed, {len(summary['skipped'])} skipped.")
    return summary
