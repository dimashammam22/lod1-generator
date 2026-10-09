# Generator LOD1

Computes building heights (**BHM**, *Building Height Model*) for **LOD1** models from LiDAR point clouds (`.las` / `.laz`) and building footprints (`.gpkg`). The result is a GeoJSON (EPSG:4326) of the footprints with a `BHM` attribute in metres.

There are two ways to run it:
- **Local web app** (`app/server.py`, or `GeneratorLOD1-Web.exe`): the UI runs in the browser while processing stays on your own PC.
- **CLI** (`python -m lod1`): for batch jobs and automation.

## Ground sources

| Mode | When to use | How it works |
|---|---|---|
| `auto`: **Auto DTM from LiDAR** | The LAS is unclassified and you have no DTM | Noise removed → points inside footprints excluded → **CSF** ground filter → DTM from a TIN (0.5 m) → BHM |
| `dtm`: **External DTM** | You have a DTM (GeoTIFF/VRT, or a folder of tiles) | BHM is measured against the DTM + a vertical-datum offset. Datum mismatches are detected automatically |
| `las`: **LAS ground (class 2)** | The LAS is already classified | Height above the nearest ground point (original algorithm) |

In the `auto` and `dtm` modes, **BHM = P95** of the height above ground of the points inside each footprint. Footprints larger than 20 m² are shrunk inward by 0.5 m, and points lower than 2 m as well as vegetation points (class 5 / multi-return) are ignored. The GeoJSON also contains `n_points` and `qc_flag` (`ok`, `low_structure`, `few_points`, `vegetation_cover`, `edge_of_data`).

## Input layout

Each LAS/LAZ uses the GPKG **in the same folder**, whatever its name. If a folder holds more than one GPKG, only the one named like the LAS is used.

```
INPUT\
├── AreaA\
│   ├── tile1.las
│   ├── tile2.las
│   └── buildings.gpkg
└── AreaB\
    ├── tile3.laz
    └── footprints.gpkg
```

The output mirrors the subfolder structure, for example `OUTPUT\AreaA\tile1_lod1.geojson` (plus `tile1_dtm.tif` in `auto` mode), along with a `run_report.json`.

## Installation

```bash
conda env create -f environment.yml
conda activate lod1gui
```

## Usage

Web app (the browser opens `http://localhost:8765` automatically):

```bash
python app/server.py
```

CLI:

```bash
python -m lod1 "D:\data\input" "D:\data\output" --ground auto --terrain 2
python -m lod1 "D:\data\input" "D:\data\output" --ground dtm --dtm "D:\dem\demnas.tif" --dtm-offset 0
python -m lod1 --help
```

As a library:

```python
from lod1 import batch_lod1_from_basedir
summary = batch_lod1_from_basedir("input", "output", ground_mode="auto")
```

## Building the exe

```bash
scripts\build_web.bat
```

The result is `dist\GeneratorLOD1-Web.exe`. This exe can be shared on its own, with no Python required. A `web\` folder placed next to the exe overrides the built-in UI, so the look can be changed without rebuilding.

## Repository layout

```
lod1/            algorithms (core.py) + CLI (__main__.py)
app/server.py    local server for the web UI
app/web/         web UI (HTML/CSS/JS)
assets/          application icon
scripts/         exe build script
```

## Technical notes
- **The web server only listens on `127.0.0.1`.** Requests with a foreign Host header and non-JSON POSTs are rejected.
- **The build script cleans `PATH`.** GDAL/PROJ DLLs from other GIS software (for example Geoflow) can conflict if they get picked up.
- **Data is never committed.** `.gitignore` excludes `*.las`, `*.laz`, `*.gpkg`, `*.tif`, `*.geojson`, `build/` and `dist/`.
