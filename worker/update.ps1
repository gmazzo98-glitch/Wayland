# Vienna Crawler Worker - atomic updater.
# This script is copied to %TEMP% before it runs, so the installation directory can be
# replaced completely. Only worker.config.json (the computer identity) survives an update.
param(
  [Parameter(Mandatory=$true)][string]$Dir,
  [Parameter(Mandatory=$true)][string]$ZipPath,
  [Parameter(Mandatory=$true)][string]$ExpectedBuild,
  [Parameter(Mandatory=$true)][int]$ParentPid,
  [Parameter(Mandatory=$true)][string]$WorkerInfoBase64
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

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

function Report-Progress([string]$Stage, [string]$Label, [int]$Percent, [hashtable]$Extra = @{}) {
  $progress = @{ stage = $Stage; label = $Label; percent = [Math]::Max(0, [Math]::Min(99, $Percent)) }
  foreach ($key in $Extra.Keys) { $progress[$key] = $Extra[$key] }
  $info = @{}
  foreach ($property in $script:WorkerInfo.PSObject.Properties) { $info[$property.Name] = $property.Value }
  $info['update_state'] = 'updating'
  $info['target_build'] = $ExpectedBuild
  $info['update_progress'] = $progress
  try {
    $body = @{ p_token = $script:WorkerConfig.token; p_info = $info } | ConvertTo-Json -Depth 12 -Compress
    $uri = $script:WorkerConfig.shimUrl.TrimEnd('/') + '/rpc/vienna_worker_heartbeat'
    Invoke-RestMethod -Method Post -Uri $uri -ContentType 'application/json' -Body $body -TimeoutSec 5 | Out-Null
  } catch { Log ('Progress heartbeat failed: ' + $_.Exception.Message) }
}

function Download-WithProgress([string]$Uri, [string]$Destination, [string]$Label, [int]$Start, [int]$End) {
  $client = [Net.Http.HttpClient]::new()
  try {
    $response = $client.GetAsync($Uri, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
    $response.EnsureSuccessStatusCode() | Out-Null
    $total = $response.Content.Headers.ContentLength
    $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
    $outputStream = [IO.File]::Create($Destination)
    try {
      $buffer = New-Object byte[] (65536)
      [long]$received = 0
      $lastReport = [DateTime]::UtcNow
      while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $outputStream.Write($buffer, 0, $count)
        $received += $count
        if (([DateTime]::UtcNow - $lastReport).TotalMilliseconds -ge 700) {
          $pct = if ($total) { $Start + [int](($End - $Start) * $received / $total) } else { $Start }
          Report-Progress 'downloading' $Label $pct @{ downloaded_bytes = $received; total_bytes = $total }
          $lastReport = [DateTime]::UtcNow
        }
      }
    } finally { $outputStream.Dispose(); $inputStream.Dispose() }
    if ($total -and $received -ne $total) { throw "Download was incomplete ($received of $total bytes)." }
    Report-Progress 'downloading' $Label $End @{ downloaded_bytes = $received; total_bytes = $total }
  } finally { $client.Dispose() }
}

function Expand-ZipWithProgress([string]$ZipPath, [string]$Destination, [string]$Label, [int]$Start, [int]$End) {
  $archive = [IO.Compression.ZipFile]::OpenRead($ZipPath)
  try {
    $total = [Math]::Max(1, $archive.Entries.Count)
    $done = 0
    foreach ($entry in $archive.Entries) {
      $target = Join-Path $Destination ($entry.FullName -replace '/', '\')
      if ($entry.FullName.EndsWith('/')) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
      } else {
        New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force | Out-Null
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
      }
      $done++
      if (($done % 10) -eq 0 -or $done -eq $total) {
        Report-Progress 'unpacking' $Label ($Start + [int](($End - $Start) * $done / $total)) @{ completed_items = $done; total_items = $total }
      }
    }
  } finally { $archive.Dispose() }
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

function Install-PackagesWithProgress([string]$NodeExe, [string]$NpmCli, [string]$Crawlers, [int]$Start, [int]$End) {
  # npm lockfileVersion 3 always keys the root package's own entry in "packages" with an empty
  # string (""), and Windows PowerShell 5.1's ConvertFrom-Json cannot build a PSCustomObject
  # member with an empty name - it throws PSArgumentException ("the value of the 'name'
  # argument is not valid"), which looked like the whole update silently hanging (the real
  # exception was only visible by running this script directly instead of detached - see
  # docs/CRAWLER_ROADMAP.md T23). That root entry is never used below anyway (the filter two
  # lines down already excludes a falsy $_.Name), so it's safe to rename before parsing rather
  # than needing a different JSON parser.
  $lockRaw = (Get-Content -LiteralPath (Join-Path $Crawlers 'package-lock.json') -Raw) -replace '"packages"\s*:\s*\{\s*""\s*:', '"packages": { "__root__":'
  $lock = $lockRaw | ConvertFrom-Json
  $packagePaths = @($lock.packages.PSObject.Properties | Where-Object { $_.Name -and -not $_.Value.dev } | ForEach-Object { $_.Name })
  $total = [Math]::Max(1, $packagePaths.Count)
  $stdout = Join-Path $env:TEMP ('vienna-npm-' + $stamp + '.out')
  $stderr = Join-Path $env:TEMP ('vienna-npm-' + $stamp + '.err')
  Report-Progress 'installing' "Installing crawler libraries (0 of $total packages)" $Start `
    @{ completed_packages = 0; total_packages = $total }
  # Start-Process -PassThru's ExitCode is unreliable here (confirmed by direct testing - see
  # docs/CRAWLER_ROADMAP.md T23): it reads back empty even after HasExited is true and the
  # process genuinely finished. System.Diagnostics.Process started directly, with -Wait,
  # does not have this problem, at the cost of not being able to poll for live progress
  # while npm runs - we only report progress before and after instead of per-package.
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $NodeExe
  $psi.Arguments = Join-Arguments @($NpmCli, 'ci', '--omit=dev', '--no-audit', '--no-fund')
  $psi.WorkingDirectory = $Crawlers
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $proc = New-Object System.Diagnostics.Process
  $proc.StartInfo = $psi
  [void]$proc.Start()
  $capturedOut = $proc.StandardOutput.ReadToEndAsync()
  $capturedErr = $proc.StandardError.ReadToEndAsync()
  $proc.WaitForExit()
  Set-Content -LiteralPath $stdout -Value $capturedOut.Result -Encoding UTF8 -ErrorAction SilentlyContinue
  Set-Content -LiteralPath $stderr -Value $capturedErr.Result -Encoding UTF8 -ErrorAction SilentlyContinue
  $exitCode = $proc.ExitCode
  $installed = 0
  foreach ($relative in $packagePaths) {
    $packageJson = Join-Path (Join-Path $Crawlers ($relative -replace '/', '\')) 'package.json'
    if (Test-Path -LiteralPath $packageJson) { $installed++ }
  }
  Report-Progress 'installing' "Installing crawler libraries ($installed of $total packages)" $End `
    @{ completed_packages = $installed; total_packages = $total }
  if ($exitCode -ne 0) {
    $tail = ($capturedErr.Result -split "`r?`n" | Select-Object -Last 20) -join ' '
    throw "Installing crawler libraries failed with exit code $exitCode`: $tail"
  }
  Remove-Item $stdout, $stderr -Force -ErrorAction SilentlyContinue
}

function Install-BrowserWithProgress([string]$NodeExe, [string]$PlaywrightCli, [string]$Crawlers) {
  $stdout = Join-Path $env:TEMP ('vienna-browser-' + $stamp + '.out')
  $stderr = Join-Path $env:TEMP ('vienna-browser-' + $stamp + '.err')
  Report-Progress 'browser' 'Downloading Chromium browser files' 81 @{ download_percent = 0 }
  # Same Start-Process -PassThru ExitCode unreliability as Install-PackagesWithProgress above
  # (see T23) - use System.Diagnostics.Process directly instead. This drops the live
  # download-percent polling (we can't peek at partial output without the same async-event
  # complexity that caused other subtle bugs elsewhere), reporting only start/end instead.
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $NodeExe
  $psi.Arguments = Join-Arguments @($PlaywrightCli, 'install', 'chromium')
  $psi.WorkingDirectory = $Crawlers
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $proc = New-Object System.Diagnostics.Process
  $proc.StartInfo = $psi
  [void]$proc.Start()
  $capturedOut = $proc.StandardOutput.ReadToEndAsync()
  $capturedErr = $proc.StandardError.ReadToEndAsync()
  $proc.WaitForExit()
  Set-Content -LiteralPath $stdout -Value $capturedOut.Result -Encoding UTF8 -ErrorAction SilentlyContinue
  Set-Content -LiteralPath $stderr -Value $capturedErr.Result -Encoding UTF8 -ErrorAction SilentlyContinue
  $exitCode = $proc.ExitCode
  Report-Progress 'browser' 'Downloading Chromium browser files (100%)' 90 @{ download_percent = 100 }
  if ($exitCode -ne 0) {
    $tail = ($capturedErr.Result -split "`r?`n" | Select-Object -Last 20) -join ' '
    throw "Downloading Chromium failed with exit code $exitCode`: $tail"
  }
  Remove-Item $stdout, $stderr -Force -ErrorAction SilentlyContinue
}

function Start-InstalledWorker([string]$InstallDir) {
  # Must not be named $Home - that collides with PowerShell's own built-in, read-only $Home
  # automatic variable (the user's profile directory) and throws "Impossibile sovrascrivere
  # la variabile Home perche e costante o di sola lettura" / VariableNotWritable the moment
  # this function is called - silently killing every update since this script was first
  # added (2026-09-29), both the pre-flight launch of the newly staged worker AND this
  # function's own use as the failure-path fallback to restart the OLD worker, so a failed
  # update couldn't even recover cleanly. Found 2026-10-12 by running the updater directly
  # (not detached) against a real stuck update - see docs/CRAWLER_ROADMAP.md T23.
  Start-Process -FilePath (Join-Path $InstallDir 'node\node.exe') `
    -ArgumentList (Join-Arguments @((Join-Path $InstallDir 'worker\worker.mjs'))) -WorkingDirectory $InstallDir -WindowStyle Hidden
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
  # Everything from here down used to run before this try block, so a bad environment (TLS
  # setup, a missing assembly, or a WorkerInfoBase64 payload mangled on the way through the
  # command line) killed the process before it ever logged a line or restarted the worker it
  # had just replaced — the install looked merely "stuck", with no diagnosis and no recovery,
  # until the app's own 20-minute grace period lapsed and it reported offline.
  try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  Add-Type -AssemblyName System.Net.Http
  $script:WorkerInfo = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($WorkerInfoBase64)) | ConvertFrom-Json
  $script:WorkerConfig = Get-Content -LiteralPath (Join-Path $Dir 'worker.config.json') -Raw | ConvertFrom-Json

  Report-Progress 'preparing' 'Preparing crawler update' 32
  try { Wait-Process -Id $ParentPid -Timeout 60 -ErrorAction SilentlyContinue } catch { }
  Stop-Workers $Dir

  New-Item -ItemType Directory -Path $stage -Force | Out-Null
  Report-Progress 'unpacking' 'Unpacking crawler update' 33
  Expand-ZipWithProgress $ZipPath $stage 'Unpacking crawler update' 33 38
  $info = Get-Content (Join-Path $stage 'VERSION.json') -Raw | ConvertFrom-Json
  if ($info.build -ne $ExpectedBuild) { throw "Downloaded build $($info.build) does not match $ExpectedBuild" }
  Copy-Item -LiteralPath (Join-Path $Dir 'worker.config.json') -Destination $stage -Force

  # Build a completely fresh runtime. Nothing from node_modules, browsers, Node, or old
  # crawler directories is copied into the new installation.
  $arch = 'x64'
  if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { $arch = 'arm64' }
  $nodeFile = $info.node.files.$arch
  $nodeZip = Join-Path $env:TEMP ('vienna-node-' + $stamp + '.zip')
  Download-WithProgress $nodeFile.url $nodeZip "Downloading Node.js $($info.node.version)" 38 50
  $actualNodeHash = (Get-FileHash $nodeZip -Algorithm SHA256).Hash.ToLower()
  if ($actualNodeHash -ne $nodeFile.sha256) { throw 'The Node.js download failed checksum verification.' }
  $nodeStage = Join-Path $env:TEMP ('vienna-node-' + $stamp)
  New-Item -ItemType Directory -Path $nodeStage -Force | Out-Null
  Expand-ZipWithProgress $nodeZip $nodeStage 'Unpacking Node.js' 50 56
  Move-Item (Get-ChildItem $nodeStage | Select-Object -First 1).FullName (Join-Path $stage 'node')
  Remove-Item $nodeStage, $nodeZip -Recurse -Force -ErrorAction SilentlyContinue

  $nodeExe = Join-Path $stage 'node\node.exe'
  $crawlers = Join-Path $stage 'crawlers'
  $npmCli = Join-Path $stage 'node\node_modules\npm\bin\npm-cli.js'
  $env:PATH = (Join-Path $stage 'node') + ';' + $env:PATH
  $env:npm_config_update_notifier = 'false'
  Install-PackagesWithProgress $nodeExe $npmCli $crawlers 56 80
  $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $stage 'browsers'
  Report-Progress 'browser' 'Downloading Chromium browser files' 81
  Install-BrowserWithProgress $nodeExe (Join-Path $crawlers 'node_modules\playwright\cli.js') $crawlers

  $env:VIENNA_WORKER_HOME = $stage
  Report-Progress 'checking' 'Testing the updated crawlers' 91
  Run $nodeExe @((Join-Path $stage 'worker\worker.mjs'), '--selftest') $stage
  Remove-Item Env:VIENNA_WORKER_HOME -ErrorAction SilentlyContinue
  Remove-Item (Join-Path $stage 'status.json') -Force -ErrorAction SilentlyContinue

  Move-Item -LiteralPath $Dir -Destination $backup
  try {
    Report-Progress 'restarting' 'Restarting the updated worker' 96
    Move-Item -LiteralPath $stage -Destination $Dir
    Start-InstalledWorker $Dir

    $connected = $false
    for ($i = 0; $i -lt 60; $i++) {
      Start-Sleep -Seconds 2
      $statusFile = Join-Path $Dir 'status.json'
      if (Test-Path $statusFile) {
        try { $status = Get-Content $statusFile -Raw | ConvertFrom-Json } catch { $status = $null }
        if ($status -and $status.state -eq 'connected' -and $status.selftest.ok) { $connected = $true; break }
        if ($status -and $status.state -eq 'connected' -and $status.selftest -and -not $status.selftest.ok) {
          throw 'The updated worker connected, but its self-test failed.'
        }
        if ($status -and $status.state -eq 'revoked') { throw 'Vienna rejected the updated worker.' }
      }
    }
    if (-not $connected) { throw 'The updated worker did not connect to Vienna within two minutes.' }
    Report-Progress 'complete' 'Crawler update complete' 100

  } catch {
    Log ('New installation failed; rolling back: ' + $_.Exception.Message)
    Stop-Workers $Dir
    if (Test-Path $Dir) { Remove-Item -LiteralPath $Dir -Recurse -Force }
    Move-Item -LiteralPath $backup -Destination $Dir
    throw
  }

  # Connection is the commit point: never roll back a healthy new installation merely
  # because antivirus briefly holds a retired file. Retry now; the worker also cleans any
  # such retired sibling on later starts.
  if (Remove-TreeWithRetry $backup) {
    Log 'Update completed and retired installation removed.'
  } else {
    try { Set-Content -LiteralPath (Join-Path $backup '.vienna-update-complete') -Value $ExpectedBuild -Encoding ASCII }
    catch { Log ('Could not mark retired folder for later cleanup: ' + $_.Exception.Message) }
    Log "Update completed; retired folder is locked and will be removed on the next worker start: $backup"
  }
  Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
} catch {
  $failure = $_.Exception.Message
  Log ('Update failed: ' + $failure)
  # Detached runs have no console, so the log is the only way to diagnose a failure after
  # the fact - keep the exception type/stack/offending line here rather than just the
  # message (this is exactly what exposed the three real bugs fixed under T23).
  Log ('Error type: ' + $_.Exception.GetType().FullName)
  Log ('Error stack: ' + $_.ScriptStackTrace)
  Log ('Error line: ' + $_.InvocationInfo.Line)
  if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
  # A failure while preparing the staged copy must not leave the existing worker stopped.
  if (Test-Path (Join-Path $Dir 'node\node.exe')) {
    @{ message = $failure; at = (Get-Date).ToUniversalTime().ToString('o') } |
      ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Dir 'update-failure.json') -Encoding UTF8
    Start-InstalledWorker $Dir
  }
  exit 1
}
exit 0
