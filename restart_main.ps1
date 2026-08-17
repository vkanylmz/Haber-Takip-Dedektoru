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

# main.py'nin bir .tmp dosyasina AKTIF yazilmakta olan stdout/stderr'inden
# YENI (Position'dan sonraki) baytlari okuyup kalici log dosyasina ekler ve
# yeni pozisyonu dondurur. FileShare.ReadWrite ile tmp dosyasi, onu ayni anda
# yazmakta olan cocuk surecle CAKISMADAN okunabiliyor.
#
# NEDEN BOYLE (basit "Start-Process -Wait, bitince kopyala" veya .NET
# Process + OutputDataReceived/ErrorDataReceived event'leri YERINE): main.py
# surekli calisan bir servis oldugu icin normalde HIC cikmiyor, "bitince
# kopyala" yaklasimi loglari surec cikana kadar hic gostermezdi. Event tabanli
# yaklasim ise denendi ama bu ortamda (venv python.exe + uzun sureli
# multi-thread uvicorn sureci) OutputDataReceived/ErrorDataReceived hicbir
# satir tetiklemedi (sebebi belirsiz, muhtemelen bu Python 3.14 kurulumunun
# python.exe'sinin kendini bir alt surece yeniden baslatmasiyla ilgili) - bu
# polling tabanli yontem basit dosya okuma/yazmaya dayandigi icin bu tuhafliktan
# etkilenmiyor ve test edilip DOGRULANDI.
function Copy-NewContent {
    param([string]$Path, [string]$Destination, [long]$Position)
    if (-not (Test-Path $Path)) { return $Position }
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    } catch {
        return $Position
    }
    try {
        if ($fs.Length -le $Position) { return $Position }
        $fs.Seek($Position, [System.IO.SeekOrigin]::Begin) | Out-Null
        $toRead = $fs.Length - $Position
        $buffer = New-Object byte[] $toRead
        $fs.Read($buffer, 0, $toRead) | Out-Null
        $text = [System.Text.Encoding]::UTF8.GetString($buffer)
        Add-Content -Path $Destination -Value $text -Encoding utf8 -NoNewline
        return $fs.Length
    } finally {
        $fs.Close()
    }
}

Write-Log "restart_main.ps1 baslatildi (izleyici dongu calisiyor)."

while ($true) {
    Write-Log "python main.py baslatiliyor..."

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $stdoutLog -Value "=== $timestamp python main.py baslatildi ===" -Encoding utf8
    Add-Content -Path $stderrLog -Value "=== $timestamp python main.py baslatildi ===" -Encoding utf8

    $tmpOut = Join-Path $appLogDir "main_stdout.tmp"
    $tmpErr = Join-Path $appLogDir "main_stderr.tmp"
    Remove-Item -Path $tmpOut, $tmpErr -Force -ErrorAction SilentlyContinue

    $proc = Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "-u main.py" `
        -WorkingDirectory $scriptDir -NoNewWindow -PassThru `
        -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr

    $outPos = 0
    $errPos = 0
    while (-not $proc.HasExited) {
        Start-Sleep -Seconds 5
        $outPos = Copy-NewContent -Path $tmpOut -Destination $stdoutLog -Position $outPos
        $errPos = Copy-NewContent -Path $tmpErr -Destination $stderrLog -Position $errPos
    }
    # Surec ciktiktan sonra tmp dosyasinda kalmis olabilecek son baytlari da yaz.
    $outPos = Copy-NewContent -Path $tmpOut -Destination $stdoutLog -Position $outPos
    $errPos = Copy-NewContent -Path $tmpErr -Destination $stderrLog -Position $errPos
    $exitCode = $proc.ExitCode
    Remove-Item -Path $tmpOut, $tmpErr -Force -ErrorAction SilentlyContinue

    Write-Log "python main.py durdu (exit code: $exitCode). 10 saniye sonra yeniden baslatilacak."
    Start-Sleep -Seconds 10
}
