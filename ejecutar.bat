@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title Casa Inteligente en Caja

cd /d "%~dp0pc"

echo ============================================================
echo   Casa Inteligente en Caja - instalacion y arranque
echo ============================================================
echo.
echo Esto deja todo listo en esta PC: entorno de Python, Ollama y
echo el modelo de respaldo. La primera vez puede tardar varios
echo minutos (descarga varios cientos de MB). Las siguientes veces
echo es casi instantaneo.
echo.

REM ------------------------------------------------------------
REM 1. Elegir interprete de Python (se prefiere 3.12: mas estable
REM    con las librerias de este proyecto que versiones mas nuevas)
REM ------------------------------------------------------------
set "PY_CMD="

where py >nul 2>&1
if %errorlevel%==0 (
    py -3.12 -c "pass" >nul 2>&1
    if !errorlevel!==0 (
        set "PY_CMD=py -3.12"
    )
)

if not defined PY_CMD (
    where python >nul 2>&1
    if !errorlevel!==0 (
        set "PY_CMD=python"
        echo [AVISO] No se encontro Python 3.12; se usara el Python por defecto del sistema.
        echo         Si la ventana se cierra sola sin avisar, instala Python 3.12 desde
        echo         https://www.python.org/downloads/ y vuelve a correr este script.
        echo.
    )
)

if not defined PY_CMD (
    echo No se encontro ningun Python instalado.
    where winget >nul 2>&1
    if !errorlevel!==0 (
        echo Instalando Python 3.12 con winget - acepta el permiso si Windows lo pide...
        winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        where py >nul 2>&1
        if !errorlevel!==0 set "PY_CMD=py -3.12"
    )
)

if not defined PY_CMD (
    echo.
    echo [ERROR] No se pudo encontrar ni instalar Python.
    echo         Instalalo manualmente desde https://www.python.org/downloads/
    echo         ^(marca "Add python.exe to PATH" durante la instalacion^) y vuelve a
    echo         correr este script.
    pause
    exit /b 1
)

echo Usando: %PY_CMD%
echo.

REM ------------------------------------------------------------
REM 2. Entorno virtual y dependencias de Python
REM ------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creando entorno virtual en pc\.venv ...
    %PY_CMD% -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo [ERROR] No se pudo crear el entorno virtual.
        pause
        exit /b 1
    )
)

echo Instalando dependencias de Python ^(faster-whisper, PySide6, etc.^)...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Fallo instalando las dependencias de pc\requirements.txt.
    pause
    exit /b 1
)
echo.

REM ------------------------------------------------------------
REM 3. Ollama + modelo de respaldo (opcional pero recomendado)
REM ------------------------------------------------------------
set "OLLAMA_MODELO=qwen2.5:1.5b"
if defined CASA_OLLAMA_MODELO set "OLLAMA_MODELO=%CASA_OLLAMA_MODELO%"

where ollama >nul 2>&1
if %errorlevel% neq 0 (
    echo Ollama no esta instalado en esta PC.
    where winget >nul 2>&1
    if !errorlevel!==0 (
        echo Instalando Ollama con winget - acepta el permiso si Windows lo pide...
        winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements
    ) else (
        echo [AVISO] No se pudo instalar Ollama automaticamente ^(no hay winget en esta PC^).
        echo         Descargalo manualmente de https://ollama.com/download si quieres el
        echo         modelo de respaldo. La casa funciona igual sin el, solo con reglas.
    )
    REM winget suele agregar Ollama al PATH hasta abrir una terminal nueva; lo intentamos
    REM igual por si ya quedo disponible en esta misma ventana.
    set "PATH=%PATH%;%LocalAppData%\Programs\Ollama"
)

where ollama >nul 2>&1
if %errorlevel%==0 (
    echo Revisando si Ollama ya esta corriendo...
    tasklist /fi "imagename eq ollama.exe" 2>nul | find /i "ollama.exe" >nul
    if errorlevel 1 (
        echo Iniciando Ollama en segundo plano...
        start "Ollama" /min cmd /c "ollama serve"
        timeout /t 4 /nobreak >nul
    )

    echo Revisando el modelo %OLLAMA_MODELO% ...
    ollama list 2>nul | find /i "%OLLAMA_MODELO%" >nul
    if errorlevel 1 (
        echo Descargando el modelo %OLLAMA_MODELO% ^(primera vez, puede tardar varios
        echo minutos segun tu internet^)...
        ollama pull %OLLAMA_MODELO%
    ) else (
        echo El modelo %OLLAMA_MODELO% ya esta descargado.
    )
) else (
    echo Sin Ollama: la casa funcionara solo con las reglas directas ^(sin el
    echo modelo de respaldo para frases raras^). No es un error.
)

echo.
echo ============================================================
echo   Todo listo. Iniciando la interfaz...
echo ============================================================
echo.
echo   Sugerencias:
echo   - Si no tienes la placa ESP32 a la mano: ejecutar.bat --demo
echo   - Cierra esta ventana para apagar la casa.
echo.

".venv\Scripts\python.exe" gui_app.py %*

echo.
echo La interfaz se cerro. Presiona una tecla para salir.
pause >nul
