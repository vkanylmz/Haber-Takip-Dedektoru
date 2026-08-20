# restart_tunnel.ps1
#
# cloudflared "quick tunnel" cokerse (veya herhangi bir sebeple kapanirsa)
# otomatik olarak yeniden baslatan basit bir izleyici dongu - restart_main.ps1
# ile AYNI desen (bkz. o dosyadaki NEDEN BOYLE notu).
#
# NOT: Quick tunnel (--url) HER yeniden baslatildiginda YENI/rastgele bir
# *.trycloudflare.com adresi uretir - kalici bir adres degildir. Guncel adres
# her seferinde data\state\tunnel_url.txt dosyasina yazilir (bu script
# tarafindan, cloudflared'in kendi ciktisindan parse edilerek). Kalici/sabit
# bir adres icin named tunnel kurulumu gerekir (bkz. README).
#
# Windows Task Scheduler'dan "sistem acilisinda" calistirilmak uzere
# tasarlandi - restart_main.ps1 ile birlikte, main.py (dashboard) ayakta
# oldugu surece disaridan erisim saglar.

$ErrorActionPreference = "Continue"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$cloudflaredExe = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$localUrl = "http://localhost:8000"

$stateDir = Join-Path $scriptDir "data\state"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$logFile = Join-Path $stateDir "restart_tunnel.log"
$tunnelUrlFile = Join-Path $stateDir "tunnel_url.txt"

$appLogDir = Join-Path $scriptDir "data\logs"
New-Item -ItemType Directory -Force -Path $appLogDir | Out-Null
$stdoutLog = Join-Path $appLogDir "cloudflared_stdout.log"

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$timestamp $Message"
    Write-Output $line
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

# cloudflared'in stdout/stderr'inde YENI bir satir bulunca (Position'dan
# sonraki baytlari okuyup) kalici log dosyasina ekler; eger o satirda
# "*.trycloudflare.com" URL'i geciyorsa tunnel_url.txt'i GUNCEL adresle
# uzerine yazar. restart_main.ps1 > Copy-NewContent ile AYNI dosya okuma
# yaklasimi (FileShare.ReadWrite, tail -f benzeri).
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

        $match = [regex]::Match($text, 'https://[a-z0-9-]+\.trycloudflare\.com')
        if ($match.Success) {
            $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
            Set-Content -Path $tunnelUrlFile -Value "$($match.Value)`n(guncellendi: $timestamp)" -Encoding utf8
            Write-Log "Tunnel adresi: $($match.Value)"
        }

        return $fs.Length
    } finally {
        $fs.Close()
    }
}

if (-not (Test-Path $cloudflaredExe)) {
    Write-Log "HATA: cloudflared.exe bulunamadi ($cloudflaredExe). Kurulumu kontrol edin."
    exit 1
}

Write-Log "restart_tunnel.ps1 baslatildi (izleyici dongu calisiyor)."

while ($true) {
    Write-Log "cloudflared tunnel baslatiliyor (hedef: $localUrl)..."

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $stdoutLog -Value "=== $timestamp cloudflared baslatildi ===" -Encoding utf8

    $tmpOut = Join-Path $appLogDir "cloudflared_stdout.tmp"
    $tmpErr = Join-Path $appLogDir "cloudflared_stderr.tmp"
    Remove-Item -Path $tmpOut, $tmpErr -Force -ErrorAction SilentlyContinue

    # cloudflared quick tunnel URL'ini de dahil TUM loglarini stderr'e yazar -
    # Start-Process, stdout/stderr'i AYNI dosyaya yonlendirmeyi kabul etmedigi
    # icin (hata: "RedirectStandardOutput and RedirectStandardError are same")
    # ayri iki gecici dosya kullanilip ikisi de AYNI kalici log dosyasina
    # (stdoutLog) ekleniyor.
    $proc = Start-Process -FilePath $cloudflaredExe `
        -ArgumentList "tunnel", "--url", $localUrl `
        -WorkingDirectory $scriptDir -NoNewWindow -PassThru `
        -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr

    $outPos = 0
    $errPos = 0
    while (-not $proc.HasExited) {
        Start-Sleep -Seconds 5
        $outPos = Copy-NewContent -Path $tmpOut -Destination $stdoutLog -Position $outPos
        $errPos = Copy-NewContent -Path $tmpErr -Destination $stdoutLog -Position $errPos
    }
    $outPos = Copy-NewContent -Path $tmpOut -Destination $stdoutLog -Position $outPos
    $errPos = Copy-NewContent -Path $tmpErr -Destination $stdoutLog -Position $errPos
    $exitCode = $proc.ExitCode
    Remove-Item -Path $tmpOut, $tmpErr -Force -ErrorAction SilentlyContinue

    Write-Log "cloudflared durdu (exit code: $exitCode). 10 saniye sonra yeniden baslatilacak."
    Start-Sleep -Seconds 10
}
