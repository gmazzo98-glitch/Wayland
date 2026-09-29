# Vienna Crawler Worker - atomic updater.
# This script is copied to %TEMP% before it runs, so the installation directory can be
# replaced completely. Only worker.config.json (the computer identity) survives an update.
param(
  [Parameter(Mandatory=$true)][string]$Dir,
  [Parameter(Mandatory=$true)][string]$ZipPath,
  [Parameter(Mandatory=$true)][string]$ExpectedBuild,
  [Parameter(Mandatory=$true)][int]$ParentPid
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
Add-Type -AssemblyName System.IO.Compression.FileSystem

$Dir = $Dir.TrimEnd('\')
$parent = Split-Path $Dir -Parent
$leaf = Split-Path $Dir -Leaf
$stamp = [guid]::NewGuid().ToString('N')
$stage = Join-Path $parent ($leaf + '-update-' + $stamp)
$backup = Join-Path $parent ($leaf + '-retired-' + $stamp)
$log = Join-Path $env:TEMP ('vienna-update-' + $stamp + '.log')

function Log([string]$Text) {
  Add-Content -LiteralPath $log -Value ((Get-Date -Format s) + '  ' + $Text) -Encoding UTF8
}

function Stop-Workers([string]$Under) {
  Get-Process node -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($Under, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { & taskkill /F /T /PID $_.Id | Out-Null }
}

function Join-Arguments([string[]]$Values) {
  return ($Values | ForEach-Object {
    if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
  }) -join ' '
}

function Run([string]$Exe, [string[]]$ToolArgs, [string]$Cwd) {
  $p = Start-Process -FilePath $Exe -ArgumentList (Join-Arguments $ToolArgs) -WorkingDirectory $Cwd -WindowStyle Hidden -Wait -PassThru
  if ($p.ExitCode -ne 0) { throw "$Exe failed with exit code $($p.ExitCode)" }
}

function Start-InstalledWorker([string]$Home) {
  Start-Process -FilePath (Join-Path $Home 'node\node.exe') `
    -ArgumentList (Join-Arguments @((Join-Path $Home 'worker\worker.mjs'))) -WorkingDirectory $Home -WindowStyle Hidden
}

function Remove-TreeWithRetry([string]$Path) {
  for ($attempt = 1; $attempt -le 10; $attempt++) {
    Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $Path)) { return $true }
    Start-Sleep -Seconds 2
  }
  return $false
}

try {
  Log "Updating $Dir to build $ExpectedBuild"
  try { Wait-Process -Id $ParentPid -Timeout 60 -ErrorAction SilentlyContinue } catch { }
  Stop-Workers $Dir

  New-Item -ItemType Directory -Path $stage -Force | Out-Null
  [IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $stage)
  $info = Get-Content (Join-Path $stage 'VERSION.json') -Raw | ConvertFrom-Json
  if ($info.build -ne $ExpectedBuild) { throw "Downloaded build $($info.build) does not match $ExpectedBuild" }
  Copy-Item -LiteralPath (Join-Path $Dir 'worker.config.json') -Destination $stage -Force

  # Build a completely fresh runtime. Nothing from node_modules, browsers, Node, or old
  # crawler directories is copied into the new installation.
  $arch = 'x64'
  if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { $arch = 'arm64' }
  $nodeFile = $info.node.files.$arch
  $nodeZip = Join-Path $env:TEMP ('vienna-node-' + $stamp + '.zip')
  Invoke-WebRequest -Uri $nodeFile.url -OutFile $nodeZip -UseBasicParsing
  $actualNodeHash = (Get-FileHash $nodeZip -Algorithm SHA256).Hash.ToLower()
  if ($actualNodeHash -ne $nodeFile.sha256) { throw 'The Node.js download failed checksum verification.' }
  $nodeStage = Join-Path $env:TEMP ('vienna-node-' + $stamp)
  [IO.Compression.ZipFile]::ExtractToDirectory($nodeZip, $nodeStage)
  Move-Item (Get-ChildItem $nodeStage | Select-Object -First 1).FullName (Join-Path $stage 'node')
  Remove-Item $nodeStage, $nodeZip -Recurse -Force -ErrorAction SilentlyContinue

  $nodeExe = Join-Path $stage 'node\node.exe'
  $crawlers = Join-Path $stage 'crawlers'
  $npmCli = Join-Path $stage 'node\node_modules\npm\bin\npm-cli.js'
  $env:PATH = (Join-Path $stage 'node') + ';' + $env:PATH
  $env:npm_config_update_notifier = 'false'
  Run $nodeExe @($npmCli, 'ci', '--omit=dev', '--no-audit', '--no-fund') $crawlers
  $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $stage 'browsers'
  Run $nodeExe @((Join-Path $crawlers 'node_modules\playwright\cli.js'), 'install', 'chromium') $crawlers

  $env:VIENNA_WORKER_HOME = $stage
  Run $nodeExe @((Join-Path $stage 'worker\worker.mjs'), '--selftest') $stage
  Remove-Item Env:VIENNA_WORKER_HOME -ErrorAction SilentlyContinue
  Remove-Item (Join-Path $stage 'status.json') -Force -ErrorAction SilentlyContinue

  Move-Item -LiteralPath $Dir -Destination $backup
  try {
    Move-Item -LiteralPath $stage -Destination $Dir
    Start-InstalledWorker $Dir

    $connected = $false
    for ($i = 0; $i -lt 60; $i++) {
      Start-Sleep -Seconds 2
      $statusFile = Join-Path $Dir 'status.json'
      if (Test-Path $statusFile) {
        try { $status = Get-Content $statusFile -Raw | ConvertFrom-Json } catch { $status = $null }
        if ($status -and $status.state -eq 'connected') { $connected = $true; break }
        if ($status -and $status.state -eq 'revoked') { throw 'Vienna rejected the updated worker.' }
      }
    }
    if (-not $connected) { throw 'The updated worker did not connect to Vienna within two minutes.' }

  } catch {
    Log ('New installation failed; rolling back: ' + $_.Exception.Message)
    Stop-Workers $Dir
    if (Test-Path $Dir) { Remove-Item -LiteralPath $Dir -Recurse -Force }
    Move-Item -LiteralPath $backup -Destination $Dir
    Start-InstalledWorker $Dir
    throw
  }

  # Connection is the commit point: never roll back a healthy new installation merely
  # because antivirus briefly holds a retired file. Retry now; the worker also cleans any
  # such retired sibling on later starts.
  if (Remove-TreeWithRetry $backup) {
    Log 'Update completed and retired installation removed.'
  } else {
    Log "Update completed; retired folder is locked and will be removed on the next worker start: $backup"
  }
  Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
} catch {
  Log ('Update failed: ' + $_.Exception.Message)
  if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
  # A failure while preparing the staged copy must not leave the existing worker stopped.
  if (Test-Path (Join-Path $Dir 'node\node.exe')) { Start-InstalledWorker $Dir }
  exit 1
}
exit 0
