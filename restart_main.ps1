# restart_main.ps1
#
# `python main.py` cokerse (veya herhangi bir sebeple kapanirsa) otomatik
# olarak yeniden baslatan basit bir izleyici dongu - 7/24 acik kalan bir
# makinede worker+bot+dashboard'un surekli ayakta kalmasini saglar.
#
# Windows Task Scheduler'dan "sistem acilisinda" calistirilmak uzere
# tasarlandi (bkz. README > Task Scheduler kurulum adimlari). Karmasik bir
# process manager (NSSM, PM2 vb.) KURULMADAN, sadece bu script + Task
# Scheduler yeterli.
#
# NOT: Ayni anda ikinci bir kopyanin calismasini onleyen kilit zaten
# main.py > src/singleton_lock.py icinde var - bu script sadece "surec
# durduysa yeniden baslat" katmanini ekliyor, kilitle CAKISMAZ: kilit,
# tuttugu surec olduğunde isletim sistemi tarafindan OTOMATIK serbest
# birakilir (bkz. o dosyadaki not), yani yeniden baslatilan main.py kilidi
# sorunsuz alir.

$ErrorActionPreference = "Continue"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$logDir = Join-Path $scriptDir "data\state"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "restart_main.log"

# python main.py'nin kendi stdout/stderr'i (ozellikle src/singleton_lock.py'nin
# bastigi "zaten calisiyor" hatasi gibi baslangic hatalari) buraya birikir -
# onceden hicbir yere yonlendirilmiyordu ve sessizce kayboluyordu.
$appLogDir = Join-Path $scriptDir "data\logs"
New-Item -ItemType Directory -Force -Path $appLogDir | Out-Null
$stdoutLog = Join-Path $appLogDir "main_stdout.log"
$stderrLog = Join-Path $appLogDir "main_stderr.log"

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$timestamp $Message"
    Write-Output $line
    # Add-Content -Encoding utf8 (Tee-Object'in varsayilan UTF-16 cikisi
    # YERINE) - log dosyasi diger araclarla (grep, cat vb.) da rahat okunsun.
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

Write-Log "restart_main.ps1 baslatildi (izleyici dongu calisiyor)."

while ($true) {
    Write-Log "python main.py baslatiliyor..."

    # Start-Process -RedirectStandardOutput/-RedirectStandardError her
    # calistirmada dosyayi SIFIRLAR (append yok) - bu yuzden gecici dosyaya
    # yazip, calisma bitince zaman damgali basligiyla birlikte kalici
    # main_stdout.log/main_stderr.log'a EKLENIYOR (Add-Content). Boylece "&"
    # cagri operatoru ile dogrudan yonlendirmede (2>>) PowerShell'in native
    # stderr'i ErrorRecord'a sarmasi sorunu da yasanmiyor.
    $tmpOut = Join-Path $appLogDir "main_stdout.tmp"
    $tmpErr = Join-Path $appLogDir "main_stderr.tmp"

    $proc = Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "main.py" `
        -WorkingDirectory $scriptDir -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
    $exitCode = $proc.ExitCode

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $stdoutLog -Value "=== $timestamp python main.py baslatildi (exit code: $exitCode) ===" -Encoding utf8
    if (Test-Path $tmpOut) {
        Get-Content -Path $tmpOut -Raw -ErrorAction SilentlyContinue | Add-Content -Path $stdoutLog -Encoding utf8
        Remove-Item -Path $tmpOut -Force -ErrorAction SilentlyContinue
    }
    Add-Content -Path $stderrLog -Value "=== $timestamp python main.py baslatildi (exit code: $exitCode) ===" -Encoding utf8
    if (Test-Path $tmpErr) {
        Get-Content -Path $tmpErr -Raw -ErrorAction SilentlyContinue | Add-Content -Path $stderrLog -Encoding utf8
        Remove-Item -Path $tmpErr -Force -ErrorAction SilentlyContinue
    }

    Write-Log "python main.py durdu (exit code: $exitCode). 10 saniye sonra yeniden baslatilacak."
    Start-Sleep -Seconds 10
}
