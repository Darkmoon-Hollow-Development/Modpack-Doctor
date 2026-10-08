@echo off
rem Builds dist\ModpackDoctor\ModpackDoctor.exe and the installer dist\ModpackDoctor-Setup-<version>.exe
rem Needs: Python 3.11+ (python.org, with "Add python.exe to PATH"). Inno Setup is installed automatically via winget if missing.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  exit /b 1
)

echo [1/3] Installing PyInstaller...
python -m pip install --upgrade pyinstaller || exit /b 1

echo [2/3] Building the program...
python -m PyInstaller --noconfirm --clean ModpackDoctor.spec || exit /b 1

for /f %%v in ('python -c "import modpack_doctor; print(modpack_doctor.__version__)"') do set VER=%%v

echo [3/3] Building the installer...
call :find_iscc
if not defined ISCC (
  echo Inno Setup not found; installing it with winget...
  winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements
  call :find_iscc
)
if not defined ISCC (
  echo.
  echo Could not find Inno Setup. Install it from https://jrsoftware.org/isdl.php and run this script again.
  echo The program itself was built: dist\ModpackDoctor\ModpackDoctor.exe
  exit /b 1
)
"%ISCC%" /DAppVersion=%VER% installer\ModpackDoctor.iss || exit /b 1

echo.
echo Done. Installer: dist\ModpackDoctor-Setup-%VER%.exe
exit /b 0

:find_iscc
set "ISCC="
for %%p in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
  if exist %%p set "ISCC=%%~p"
)
if not defined ISCC (
  for /f "delims=" %%i in ('where iscc 2^>nul') do set "ISCC=%%i"
)
exit /b 0
