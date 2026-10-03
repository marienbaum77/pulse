#Requires -Version 5.1
# Автоустановка Pulse на Windows: Docker Desktop -> .env -> запуск стека -> браузер.
# Запуск: двойной клик по install.bat или `powershell -ExecutionPolicy Bypass -File install.ps1`
$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "`nОшибка: $m" -ForegroundColor Red; Read-Host 'Нажмите Enter для выхода'; exit 1 }
function RandHex($bytes) {
    $b = New-Object byte[] $bytes
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    -join ($b | ForEach-Object { $_.ToString('x2') })
}
function DockerReady { try { docker info *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false } }

# 1. Docker
Step 'Проверка Docker'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail 'Не найден winget. Установите Docker Desktop вручную: https://www.docker.com/products/docker-desktop/ и запустите скрипт снова.'
    }
    Write-Host 'Устанавливаю Docker Desktop (потребуется подтверждение администратора)...'
    winget install -e --id Docker.DockerDesktop --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $dockerExe = "$env:ProgramFiles\Docker\Docker\resources\bin"
    if (Test-Path $dockerExe) { $env:Path += ";$dockerExe" }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Fail 'Docker установлен, но требуется перезагрузка компьютера. Перезагрузитесь, запустите Docker Desktop и снова запустите install.bat.'
    }
}
if (-not (DockerReady)) {
    $desktop = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $desktop) { Write-Host 'Запускаю Docker Desktop...'; Start-Process $desktop }
    Write-Host 'Жду, пока Docker запустится (до 5 минут; при первом запуске примите лицензионное соглашение в окне Docker)...'
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) { if (DockerReady) { $ok = $true; break }; Start-Sleep -Seconds 5 }
    if (-not $ok) { Fail 'Docker не запустился. Откройте Docker Desktop вручную, дождитесь статуса Running и запустите скрипт снова. Если установка была только что, сначала перезагрузите компьютер.' }
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail 'Не найден docker compose. Обновите Docker Desktop.' }

# 2. .env
Step 'Настройка .env'
if (Test-Path .env) {
    Write-Host '.env уже существует — оставляю без изменений.'
} else {
    $email = Read-Host 'Email администратора [admin@example.com]'
    if (-not $email) { $email = 'admin@example.com' }
    $adminPass = Read-Host 'Пароль администратора (Enter — сгенерировать)'
    $generated = $false
    if (-not $adminPass) { $adminPass = RandHex 8; $generated = $true }
    $apiKey = Read-Host 'API-ключ чат-модели Groq (Enter — пропустить, будет упрощённая генерация)'
    $tg = Read-Host 'Токен Telegram-бота от @BotFather (Enter — пропустить)'

    $values = @{
        POSTGRES_PASSWORD  = RandHex 16
        SECRET_KEY         = RandHex 32
        ADMIN_EMAIL        = $email
        ADMIN_PASSWORD     = $adminPass
        LLM_API_KEY        = $apiKey
        TELEGRAM_BOT_TOKEN = $tg
    }
    if (-not $apiKey) { $values['LLM_PROVIDER'] = 'stub' }

    $lines = Get-Content .env.example -Encoding UTF8 | ForEach-Object {
        $line = $_
        foreach ($k in $values.Keys) {
            if ($line -match "^$k=") { $line = "$k=$($values[$k])"; break }
        }
        $line
    }
    [IO.File]::WriteAllText("$PSScriptRoot\.env", (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
    Write-Host '.env создан (секреты сгенерированы). Не публикуйте этот файл.'
    if ($generated) { Write-Host "Пароль администратора: $adminPass  (запишите его)" -ForegroundColor Yellow }
}

# 3. Запуск
Step 'Сборка и запуск (первый раз занимает несколько минут)'
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Fail 'docker compose up завершился с ошибкой. Смотрите вывод выше.' }

Step 'Загрузка модели эмбеддингов bge-m3 (~1.2 ГБ, один раз)'
docker compose exec -T ollama ollama pull bge-m3
if ($LASTEXITCODE -ne 0) { Fail 'Не удалось загрузить bge-m3. Проверьте интернет и повторите: docker compose exec ollama ollama pull bge-m3' }

docker compose exec -T api python -m app.seed

# 4. Ожидание и открытие интерфейса
$port = 8080
$m = Select-String -Path .env -Pattern '^PULSE_PORT=(\d+)' | Select-Object -First 1
if ($m) { $port = $m.Matches[0].Groups[1].Value }
$url = "http://localhost:$port"

Step "Жду готовности интерфейса на $url"
$up = $false
for ($i = 0; $i -lt 60; $i++) {
    try { if ((Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { $up = $true; break } } catch {}
    Start-Sleep -Seconds 3
}
if (-not $up) { Fail "Интерфейс не отвечает. Смотрите логи: docker compose logs --tail=100 api web" }

Start-Process $url
Write-Host "`nГотово. Pulse открыт: $url" -ForegroundColor Green
Write-Host 'Остановить: docker compose stop   Запустить снова: docker compose up -d'
Write-Host 'Pulse работает, пока включены компьютер и Docker Desktop.'
Read-Host 'Нажмите Enter для выхода'
