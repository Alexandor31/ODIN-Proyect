@echo off
REM ----------------------------------------------------------------
REM setup.bat - Bootstrap para DANTE Industrial en Windows
REM Crea un entorno virtual (.venv) e instala las dependencias.
REM ----------------------------------------------------------------
setlocal

set PROJECT_DIR=%~dp0
cd /d "%PROJECT_DIR%"

set VENV_DIR=%PROJECT_DIR%.venv
if not defined PYTHON set PYTHON=python

echo ==^> DANTE Industrial . Setup (Windows)
echo     Proyecto : %PROJECT_DIR%
echo     Python   :
%PYTHON% --version

REM 1) Crear venv si no existe
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo ==^> Creando entorno virtual...
    %PYTHON% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] No se pudo crear el entorno virtual.
        exit /b 1
    )
)

call "%VENV_DIR%\Scripts\activate.bat"

REM 2) Actualizar pip
echo ==^> Actualizando pip...
python -m pip install --upgrade pip --quiet

REM 3) Instalar dependencias
echo ==^> Instalando requirements.txt...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Fallo la instalacion de dependencias.
    exit /b 1
)

REM 4) Verificar
echo ==^> Verificando instalacion...
python -c "import importlib,sys;faltan=[(m,str(e)) for m in ['fastapi','uvicorn','pydantic','pymodbus','requests','httpx'] if (lambda:None)() or True for e in [None]]; mods=[];[mods.append((m,str(__import__('importlib').import_module(m).__name__ if False else None)) ) for m in []];[sys.exit(0) if all((lambda:__import__(m) for m in ['fastapi','uvicorn','pydantic','pymodbus','requests','httpx'])) else 1]"
if errorlevel 1 (
    echo [ERROR] Faltan dependencias. Revisa los mensajes anteriores.
    exit /b 1
)

echo.
echo OK . Entorno listo.
echo.
echo Para arrancar el servidor:
echo     .venv\Scripts\activate
echo     python main.py
echo.
echo Y abre http://localhost:8000
endlocal
