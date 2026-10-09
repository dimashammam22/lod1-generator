# Generator LOD1

Menghitung tinggi bangunan (**BHM**, *Building Height Model*) untuk model **LOD1** dari point cloud LiDAR (`.las` / `.laz`) dan footprint bangunan (`.gpkg`). Hasilnya GeoJSON (EPSG:4326) berisi footprint dengan atribut `BHM` dalam meter.

Tersedia dua cara menjalankan:
- **Aplikasi web lokal** (`app/server.py`, atau `GeneratorLOD1-Web.exe`): UI di browser, proses tetap berjalan di PC sendiri.
- **CLI** (`python -m lod1`): untuk batch dan otomatisasi.

## Sumber tanah (ground source)

| Mode | Kapan dipakai | Cara kerja |
|---|---|---|
| `auto` — **Auto DTM from LiDAR** | LAS belum terklasifikasi, tidak punya DTM | Noise dibuang → titik di footprint dikeluarkan → ground filter **CSF** → DTM dari TIN (0.5 m) → BHM |
| `dtm` — **External DTM** | Punya DTM (GeoTIFF/VRT, atau folder tile) | BHM diukur terhadap DTM + offset datum vertikal. Selisih datum dideteksi otomatis |
| `las` — **LAS ground (class 2)** | LAS sudah terklasifikasi | Tinggi terhadap titik ground terdekat (algoritma awal) |

Pada mode `auto` dan `dtm`: **BHM = P95** dari tinggi di atas tanah untuk titik di dalam footprint. Footprint > 20 m² dikecilkan 0.5 m ke dalam, titik < 2 m dan titik vegetasi (kelas 5 / multi-return) diabaikan. GeoJSON juga berisi `n_points` dan `qc_flag` (`ok`, `low_structure`, `few_points`, `vegetation_cover`, `edge_of_data`).

## Struktur data input

Setiap LAS/LAZ memakai GPKG yang ada **di folder yang sama**, apa pun namanya. Kalau dalam satu folder ada lebih dari satu GPKG, hanya GPKG yang namanya sama dengan LAS yang dipakai.

```
INPUT\
├── AreaA\
│   ├── tile1.las
│   ├── tile2.las
│   └── bangunan.gpkg
└── AreaB\
    ├── tile3.laz
    └── footprint.gpkg
```

Output mengikuti struktur subfolder: `OUTPUT\AreaA\tile1_lod1.geojson` (+ `tile1_dtm.tif` pada mode `auto`), ditambah `run_report.json`.

## Instalasi

```bash
conda env create -f environment.yml
conda activate lod1gui
```

## Menjalankan

Aplikasi web (browser terbuka otomatis di `http://localhost:8765`):

```bash
python app/server.py
```

CLI:

```bash
python -m lod1 "D:\data\input" "D:\data\output" --ground auto --terrain 2
python -m lod1 "D:\data\input" "D:\data\output" --ground dtm --dtm "D:\dem\demnas.tif" --dtm-offset 0
python -m lod1 --help
```

Sebagai library:

```python
from lod1 import batch_lod1_from_basedir
summary = batch_lod1_from_basedir("input", "output", ground_mode="auto")
```

## Build exe

```bash
scripts\build_web.bat
```

Hasilnya ada di `dist\GeneratorLOD1-Web.exe`. Exe ini bisa dibagikan sendirian, tanpa perlu Python. Folder `web\` yang diletakkan di samping exe akan menimpa UI bawaan, jadi tampilan bisa diubah tanpa build ulang.

## Struktur repository

```
lod1/            algoritma (core.py) + CLI (__main__.py)
app/server.py    server lokal untuk UI web
app/web/         UI web (HTML/CSS/JS)
assets/          ikon aplikasi
scripts/         skrip build exe
```

## Catatan teknis
- **Server web hanya mendengarkan `127.0.0.1`.** Request dengan Host asing dan POST non-JSON ditolak.
- **Skrip build membersihkan `PATH`.** DLL GDAL/PROJ dari software GIS lain (misalnya Geoflow) bisa bentrok kalau ikut terbaca.
- **Data tidak di-commit.** `.gitignore` mengecualikan `*.las`, `*.laz`, `*.gpkg`, `*.tif`, `*.geojson`, `build/`, dan `dist/`.
