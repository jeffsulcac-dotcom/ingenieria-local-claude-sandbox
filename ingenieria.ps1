param(
    [Parameter(Position = 0)]
    [ValidateSet("iniciar", "estado", "detener")]
    [string]$Accion = "estado"
)

$ErrorActionPreference = "Stop"

$Raiz = Split-Path -Parent $MyInvocation.MyCommand.Path
$Infraestructura = Join-Path $Raiz "infraestructura"
$Compose = Join-Path $Infraestructura "compose.yml"
$Env = Join-Path $Infraestructura ".env"
$Python = Join-Path $Raiz ".venv\Scripts\python.exe"

$PuertoAplicacion = 8765
$UrlAplicacion = "http://127.0.0.1:$PuertoAplicacion"
$UrlSalud = "$UrlAplicacion/salud"
$UrlN8n = "http://127.0.0.1:5678"


function Mostrar-Titulo {
    Write-Host ""
    Write-Host "==========================================" -ForegroundColor DarkGray
    Write-Host "          INGENIERÍA LOCAL" -ForegroundColor White
    Write-Host "==========================================" -ForegroundColor DarkGray
    Write-Host ""
}


function Probar-Aplicacion {
    try {
        $respuesta = Invoke-RestMethod `
            -Uri $UrlSalud `
            -TimeoutSec 2

        return ($respuesta.estado -eq "activo")
    }
    catch {
        return $false
    }
}


function Probar-N8n {
    try {
        $respuesta = Invoke-WebRequest `
            -Uri $UrlN8n `
            -UseBasicParsing `
            -TimeoutSec 2

        return ($respuesta.StatusCode -eq 200)
    }
    catch {
        return $false
    }
}


function Probar-Docker {
    try {
        docker info *> $null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}


function Iniciar-Docker {
    if (Probar-Docker) {
        Write-Host "[OK] Docker ya está activo." -ForegroundColor Green
        return
    }

    Write-Host "[..] Iniciando Docker Desktop..." -ForegroundColor Yellow

    $RutasDockerDesktop = @(
        "C:\Program Files\Docker\Docker\Docker Desktop.exe",
        "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe"
    )

    $DockerDesktop = $RutasDockerDesktop | Where-Object { Test-Path $_ } | Select-Object -First 1

    if (-not $DockerDesktop) {
        throw "No se encontró Docker Desktop."
    }

    Start-Process $DockerDesktop

    $Limite = 60

    for ($i = 0; $i -lt $Limite; $i++) {

        Start-Sleep -Seconds 1

        if (Probar-Docker) {
            Write-Host "[OK] Docker iniciado." -ForegroundColor Green
            return
        }
    }

    throw "Docker no inició dentro del tiempo esperado."
}


function Iniciar-Infraestructura {

    Write-Host "[..] Verificando infraestructura local..." -ForegroundColor Yellow

    Iniciar-Docker

    Push-Location $Infraestructura

    try {
        docker compose `
            --env-file $Env `
            -f $Compose `
            up -d

        if ($LASTEXITCODE -ne 0) {
            throw "No se pudo iniciar la infraestructura Docker."
        }
    }
    finally {
        Pop-Location
    }

    Write-Host "[OK] PostgreSQL / Redis / n8n iniciados." -ForegroundColor Green
}


function Iniciar-Aplicacion {

    if (Probar-Aplicacion) {
        Write-Host "[OK] Ingeniería Local ya está activa." -ForegroundColor Green
        return
    }

    if (-not (Test-Path $Python)) {
        throw "No se encontró el entorno Python local."
    }

    Write-Host "[..] Iniciando aplicación..." -ForegroundColor Yellow

    Start-Process `
        -FilePath $Python `
        -ArgumentList @(
            "-m",
            "uvicorn",
            "aplicacion.ingenieria_app.servidor:app",
            "--host",
            "127.0.0.1",
            "--port",
            "$PuertoAplicacion"
        ) `
        -WorkingDirectory $Raiz `
        -WindowStyle Hidden

    for ($i = 0; $i -lt 15; $i++) {

        Start-Sleep -Seconds 1

        if (Probar-Aplicacion) {
            Write-Host "[OK] Aplicación iniciada." -ForegroundColor Green
            return
        }
    }

    throw "La aplicación no respondió dentro del tiempo esperado."
}


function Mostrar-Estado {

    Mostrar-Titulo

    if (Probar-Docker) {
        Write-Host "Docker ............... ACTIVO" -ForegroundColor Green
    }
    else {
        Write-Host "Docker ............... DETENIDO" -ForegroundColor Red
    }

    if (Probar-N8n) {
        Write-Host "n8n .................. ACTIVO" -ForegroundColor Green
    }
    else {
        Write-Host "n8n .................. DETENIDO" -ForegroundColor Red
    }

    if (Probar-Aplicacion) {
        Write-Host "Ingeniería Local ..... ACTIVA" -ForegroundColor Green
    }
    else {
        Write-Host "Ingeniería Local ..... DETENIDA" -ForegroundColor Red
    }

    Write-Host ""
    Write-Host "Aplicación: $UrlAplicacion"
    Write-Host "n8n:        $UrlN8n"
    Write-Host ""
}


function Detener-Aplicacion {

    $conexiones = Get-NetTCPConnection `
        -LocalPort $PuertoAplicacion `
        -State Listen `
        -ErrorAction SilentlyContinue

    if ($conexiones) {

        $procesos = $conexiones |
            Select-Object -ExpandProperty OwningProcess -Unique

        foreach ($pidProceso in $procesos) {

            try {
                Stop-Process `
                    -Id $pidProceso `
                    -Force `
                    -ErrorAction Stop

                Write-Host "[OK] Aplicación detenida." -ForegroundColor Green
            }
            catch {
                Write-Host "[AVISO] No se pudo detener PID $pidProceso." -ForegroundColor Yellow
            }
        }
    }
    else {
        Write-Host "[OK] La aplicación ya estaba detenida." -ForegroundColor Green
    }
}


function Detener-Infraestructura {

    if (-not (Probar-Docker)) {
        Write-Host "[OK] Docker ya está detenido." -ForegroundColor Green
        return
    }

    Push-Location $Infraestructura

    try {
        docker compose `
            --env-file $Env `
            -f $Compose `
            stop
    }
    finally {
        Pop-Location
    }

    Write-Host "[OK] Servicios Docker detenidos." -ForegroundColor Green
}


switch ($Accion) {

    "iniciar" {

        Mostrar-Titulo

        Iniciar-Infraestructura
        Iniciar-Aplicacion

        Write-Host ""
        Write-Host "SISTEMA LISTO" -ForegroundColor Green
        Write-Host ""

        Write-Host "Aplicación:"
        Write-Host $UrlAplicacion -ForegroundColor Cyan

        Write-Host ""
        Write-Host "Orquestación n8n:"
        Write-Host $UrlN8n -ForegroundColor Cyan

        Write-Host ""

        Start-Process $UrlAplicacion
    }


    "estado" {

        Mostrar-Estado
    }


    "detener" {

        Mostrar-Titulo

        Detener-Aplicacion
        Detener-Infraestructura

        Write-Host ""
        Write-Host "INGENIERÍA LOCAL DETENIDA" -ForegroundColor Yellow
        Write-Host ""
    }
}
