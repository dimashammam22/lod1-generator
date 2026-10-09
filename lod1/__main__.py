"""Command line: process every LAS/LAZ + GPKG pair under a folder.

    python -m lod1 INPUT_DIR OUTPUT_DIR [--ground auto|dtm|las] [options]
"""
import argparse

from .core import batch_lod1_from_basedir


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='python -m lod1',
        description='LOD1 building heights (BHM) from LiDAR + building footprints. '
                    'Each LAS/LAZ uses the GPKG in its own folder.')
    p.add_argument('input', help='folder with LAS/LAZ + GPKG (subfolders allowed)')
    p.add_argument('output', help='folder for <name>_lod1.geojson (and <name>_dtm.tif)')
    p.add_argument('--ground', choices=['auto', 'dtm', 'las'], default='auto',
                   help='auto: DTM from the LAS via CSF (default); dtm: external DTM; '
                        'las: classified ground (class 2)')
    p.add_argument('--percentile', type=int, default=95, help='BHM percentile (default 95)')
    p.add_argument('--min-height', type=float, default=2.0,
                   help='ignore points lower than this above ground, m (auto/dtm, default 2.0)')
    p.add_argument('--inner-buffer', type=float, default=0.5,
                   help='shrink footprints > 20 m² by this, m (auto/dtm, default 0.5)')
    p.add_argument('--dtm-res', type=float, default=0.5, help='auto: DTM cell size, m (default 0.5)')
    p.add_argument('--terrain', type=int, choices=[1, 2, 3], default=2,
                   help='auto: CSF rigidness — 1 steep, 2 hilly (default), 3 flat')
    p.add_argument('--dtm', help='dtm: DTM raster file or folder of tiles')
    p.add_argument('--dtm-offset', type=float, default=0.0,
                   help='dtm: value added to the DTM to match the LAS vertical datum, m')
    p.add_argument('--epsg', type=int, default=4326, help='output CRS (default 4326)')
    a = p.parse_args(argv)
    if a.ground == 'dtm' and not a.dtm:
        p.error('--ground dtm needs --dtm PATH')

    summary = batch_lod1_from_basedir(
        basedir=a.input,
        output_dir=a.output,
        ground_mode=a.ground,
        percentile=a.percentile,
        min_height=a.min_height,
        inner_buffer=a.inner_buffer,
        dtm_res=a.dtm_res,
        rigidness=a.terrain,
        dtm_path=a.dtm,
        dtm_offset=a.dtm_offset,
        target_epsg=a.epsg,
        resolution=0.1,
    )
    for item in summary['failed']:
        print(f"  failed  {item['name']}: {item['reason']}")
    for item in summary['skipped']:
        print(f"  skipped {item['name']}: {item['reason']}")
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
