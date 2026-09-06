param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Binary
)

$ErrorActionPreference = 'Stop'
$Source = (Resolve-Path -LiteralPath $Binary).Path
if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
    throw "MediaDL binary not found: $Binary"
}

$BinDir = Join-Path $env:LOCALAPPDATA 'MediaDL\bin'
$Target = Join-Path $BinDir 'mdl.exe'
$Temp = Join-Path $BinDir ('.mdl.install.' + $PID + '.exe')
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
Copy-Item -LiteralPath $Source -Destination $Temp -Force

try {
    & $Temp --version | Out-Null
    Move-Item -LiteralPath $Temp -Destination $Target -Force
}
finally {
    Remove-Item -LiteralPath $Temp -Force -ErrorAction SilentlyContinue
}

$UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$Parts = @()
if ($UserPath) {
    $Parts = $UserPath.Split(';') | Where-Object { $_ }
}
if (-not ($Parts | Where-Object { $_.TrimEnd('\') -ieq $BinDir.TrimEnd('\') })) {
    $NewPath = if ($UserPath) { "$UserPath;$BinDir" } else { $BinDir }
    [Environment]::SetEnvironmentVariable('Path', $NewPath, 'User')
    Write-Host "User PATH updated for future terminals."
}
if (-not (($env:Path.Split(';')) | Where-Object { $_.TrimEnd('\') -ieq $BinDir.TrimEnd('\') })) {
    $env:Path = "$env:Path;$BinDir"
}

Write-Host "MediaDL installed: $Target"
Write-Host "Use: mdl --help"
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "Required: install FFmpeg before downloading so MediaDL can merge/convert media."
}
