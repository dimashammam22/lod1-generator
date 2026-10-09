@echo off
REM Build dist\GeneratorLOD1-Web.exe (local server + browser UI).
REM Needs the conda env from environment.yml:  conda env create -f environment.yml
REM The app\web folder is bundled; a web\ folder placed next to the exe overrides it.
cd /d "%~dp0.."
set "ROOT=%CD%"
call conda activate lod1gui || exit /b 1

REM Clean PATH: other GIS software on PATH (Geoflow, roofer, base conda) ships
REM GDAL/PROJ DLLs that would otherwise be picked up and bundled by mistake.
set "PATH=%CONDA_PREFIX%;%CONDA_PREFIX%\Library\bin;%CONDA_PREFIX%\Scripts;%SystemRoot%\system32;%SystemRoot%"

python -m PyInstaller --noconfirm --onefile --console --name GeneratorLOD1-Web ^
  --icon "%ROOT%\assets\app.ico" ^
  --add-data "%ROOT%\app\web;web" ^
  --paths "%ROOT%" --hidden-import lod1.core ^
  --collect-all pyogrio --collect-all pyproj --collect-all shapely --collect-data geopandas ^
  --collect-all rasterio --collect-all lazrs --collect-all CSF ^
  --exclude-module PySide6 --exclude-module matplotlib --exclude-module IPython ^
  --workpath "%ROOT%\build" --specpath "%ROOT%\build" --distpath "%ROOT%\dist" ^
  "%ROOT%\app\server.py" || exit /b 1

echo.
echo Done: dist\GeneratorLOD1-Web.exe
