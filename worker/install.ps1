# Vienna Crawler Worker - installer.
# Runs on the helper's own Windows PC from the self-extracting Setup file. No admin rights needed:
# everything goes into %LOCALAPPDATA%\ViennaCrawlers (its own copy of Node.js, the crawlers, and a headless browser).
# Plain ASCII on purpose: this text is embedded in a .bat and must survive any code page.
#
# Normal use: double-click the Setup file. For testing: Setup.bat -Test -Dir C:\some\folder
param([string]$Dir, [switch]$Test)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is many times faster without its progress bar
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
Add-Type -AssemblyName System.IO.Compression.FileSystem

$Source = $env:VIENNA_SETUP_SOURCE
if (-not $Dir) { $Dir = Join-Path $env:LOCALAPPDATA 'ViennaCrawlers' }
$Dir = $Dir.TrimEnd('\')
$TotalSteps = 6
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$LogFile = Join-Path $Dir 'install.log'
Set-Content -Path $LogFile -Value ("Vienna Crawler Setup " + (Get-Date -Format s)) -Encoding ASCII

function Say([string]$Text, [string]$Color = 'Gray') {
  Write-Host $Text -ForegroundColor $Color
  Add-Content -Path $LogFile -Value $Text -Encoding ASCII
}
function Step([int]$N, [string]$Text) {
  Say ''
  Say ("  [$N/$TotalSteps] $Text") 'Cyan'
}
function Show-Box([string]$Text, [string]$Title, [string]$Icon) {
  if ($Test) { return }
  try {
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show($Text, $Title, 'OK', $Icon)
  } catch { }
}

# Runs one program, shows a dot every couple of seconds so it never looks frozen, logs its output.
function Invoke-Tool([string]$Exe, [string[]]$ToolArgs, [string]$Cwd) {
  $argString = ($ToolArgs | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' '
  $outFile = [IO.Path]::GetTempFileName()
  $errFile = [IO.Path]::GetTempFileName()
  try {
    $p = Start-Process -FilePath $Exe -ArgumentList $argString -WorkingDirectory $Cwd -NoNewWindow -PassThru `
         -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $null = $p.Handle   # keeps ExitCode readable after the process ends
    while (-not $p.HasExited) { Write-Host -NoNewline '.'; Start-Sleep -Seconds 2 }
    $p.WaitForExit()
    Write-Host ''
    $out = (Get-Content $outFile -Raw -ErrorAction SilentlyContinue)
    $err = (Get-Content $errFile -Raw -ErrorAction SilentlyContinue)
    Add-Content -Path $LogFile -Value ("$Exe $argString`r`n$out`r`n$err") -Encoding ASCII
    return [pscustomobject]@{ Code = $p.ExitCode; Out = $out; Err = $err }
  } finally {
    Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue
  }
}

function Get-Tail($Result) {
  return ((($Result.Err + "`n" + $Result.Out) -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last 6) -join ' | '
}

function Stop-Worker {
  Get-Process node -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($Dir, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { & taskkill /F /T /PID $_.Id | Out-Null }
}

function New-Shortcut([string]$Path, [string]$Target, [string]$Arguments, [string]$WorkDir, [string]$Description) {
  $shell = New-Object -ComObject WScript.Shell
  $s = $shell.CreateShortcut($Path)
  $s.TargetPath = $Target
  $s.Arguments = $Arguments
  $s.WorkingDirectory = $WorkDir
  $s.Description = $Description
  $s.WindowStyle = 7
  $s.Save()
}

try {
  Clear-Host
  Say ''
  Say '  ============================================================' 'Green'
  Say '    Vienna Crawler Setup' 'Green'
  Say '  ============================================================' 'Green'
  Say '  This installs everything needed to run the Vienna crawlers on'
  Say '  this computer. It takes a few minutes (about 300 MB to download).'
  Say '  You do not need to click anything. Please leave this window open.'

  # ---- 1. unpack ------------------------------------------------------------------------
  Step 1 'Unpacking the crawlers...'
  if (-not $Source -or -not (Test-Path $Source)) { throw 'The setup file could not find itself. Please download it again.' }
  if ($Source.ToLower().EndsWith('.zip')) {
    $zipBytes = [IO.File]::ReadAllBytes($Source)
  } else {
    $text = [IO.File]::ReadAllText($Source)
    $at = $text.LastIndexOf("`n" + '##' + 'ZIP')
    if ($at -lt 0) { throw 'The setup file is damaged (no package inside). Please download it again.' }
    $zipBytes = [Convert]::FromBase64String(($text.Substring($at + 6) -replace '\s', ''))
  }
  $zipPath = Join-Path $env:TEMP ('vienna-bundle-' + [guid]::NewGuid().ToString('N') + '.zip')
  $stage = Join-Path $env:TEMP ('vienna-stage-' + [guid]::NewGuid().ToString('N'))
  [IO.File]::WriteAllBytes($zipPath, $zipBytes)
  [IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $stage)
  Remove-Item $zipPath -Force -ErrorAction SilentlyContinue

  $info = Get-Content (Join-Path $stage 'VERSION.json') -Raw | ConvertFrom-Json
  if (-not (Test-Path (Join-Path $stage 'worker.config.json'))) { throw 'The setup file has no connection details. Download it again from the Vienna app.' }

  Stop-Worker
  Start-Sleep -Milliseconds 800
  foreach ($old in @($info.crawlers)) { Remove-Item (Join-Path $Dir "crawlers\$old") -Recurse -Force -ErrorAction SilentlyContinue }
  Remove-Item (Join-Path $Dir 'worker') -Recurse -Force -ErrorAction SilentlyContinue
  Copy-Item -Path (Join-Path $stage '*') -Destination $Dir -Recurse -Force
  Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
  Say ("  Unpacked version " + $info.version + " (build " + $info.build + ").")

  # ---- 2. Node.js (its own private copy; never touches an existing installation) -------------
  Step 2 'Getting Node.js (the engine the crawlers run on)...'
  $arch = 'x64'
  if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { $arch = 'arm64' }
  elseif ($env:PROCESSOR_ARCHITECTURE -eq 'x86' -and -not $env:PROCESSOR_ARCHITEW6432) { throw '32-bit Windows is not supported.' }
  $nodeExe = Join-Path $Dir 'node\node.exe'
  $want = $info.node.version
  $have = ''
  if (Test-Path $nodeExe) { try { $have = (& $nodeExe --version) } catch { $have = '' } }
  if ($have -eq $want) {
    Say "  Node.js $want is already here."
  } else {
    $file = $info.node.files.$arch
    $nodeZip = Join-Path $env:TEMP "vienna-node-$want-$arch.zip"
    Say "  Downloading Node.js $want ..."
    Invoke-WebRequest -Uri $file.url -OutFile $nodeZip -UseBasicParsing
    $sha = (Get-FileHash $nodeZip -Algorithm SHA256).Hash.ToLower()
    if ($sha -ne $file.sha256) { throw 'The Node.js download did not match its published checksum, so it was discarded. Try again.' }
    $nodeStage = Join-Path $env:TEMP ('vienna-nodestage-' + [guid]::NewGuid().ToString('N'))
    [IO.Compression.ZipFile]::ExtractToDirectory($nodeZip, $nodeStage)
    Remove-Item (Join-Path $Dir 'node') -Recurse -Force -ErrorAction SilentlyContinue
    Move-Item (Get-ChildItem $nodeStage | Select-Object -First 1).FullName (Join-Path $Dir 'node')
    Remove-Item $nodeStage, $nodeZip -Recurse -Force -ErrorAction SilentlyContinue
    Say "  Node.js $want installed."
  }
  $env:PATH = (Join-Path $Dir 'node') + ';' + $env:PATH
  $env:npm_config_update_notifier = 'false'

  # ---- 3. crawler libraries ---------------------------------------------------------------------
  Step 3 'Installing the crawler libraries (the longest step)...'
  $crawlers = Join-Path $Dir 'crawlers'
  $lockHash = (Get-FileHash (Join-Path $crawlers 'package-lock.json') -Algorithm SHA256).Hash
  $hashFile = Join-Path $crawlers 'node_modules\.vienna-lock-hash'
  $haveHash = ''
  if (Test-Path $hashFile) { $haveHash = (Get-Content $hashFile -Raw).Trim() }
  if ($haveHash -eq $lockHash -and (Test-Path (Join-Path $crawlers 'node_modules\crawlee\package.json'))) {
    Say '  Already installed and up to date.'
  } else {
    $npmCli = Join-Path $Dir 'node\node_modules\npm\bin\npm-cli.js'
    $r = Invoke-Tool $nodeExe @($npmCli, 'ci', '--omit=dev', '--no-audit', '--no-fund') $crawlers
    if ($r.Code -ne 0) { throw ('Installing the crawler libraries failed: ' + (Get-Tail $r)) }
    Set-Content -Path $hashFile -Value $lockHash -Encoding ASCII
    Say '  Done.'
  }

  # ---- 4. headless browser ----------------------------------------------------------------------------
  Step 4 'Installing the headless browser the crawlers use...'
  $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $Dir 'browsers'
  $pwCli = Join-Path $crawlers 'node_modules\playwright\cli.js'
  $r = Invoke-Tool $nodeExe @($pwCli, 'install', 'chromium') $crawlers
  if ($r.Code -ne 0) { throw ('Downloading the browser failed (a firewall or proxy may be blocking it): ' + (Get-Tail $r)) }
  Say '  Done.'

  # ---- 5. self-test --------------------------------------------------------------------------------------
  Step 5 'Checking that everything works...'
  $r = Invoke-Tool $nodeExe @((Join-Path $Dir 'worker\worker.mjs'), '--selftest') $Dir
  foreach ($line in ($r.Out -split "`r?`n")) { if ($line.Trim()) { Say ("  " + $line.Trim()) } }
  if ($r.Code -ne 0) { throw 'The self-test found a problem (see above). Nothing is wrong with your computer; please send a screenshot of this window.' }

  # ---- 6. start it, and make it start with Windows ----------------------------------------------------
  Step 6 'Starting the worker and connecting to Vienna...'
  if (-not $Test) {
    $startup = [Environment]::GetFolderPath('Startup')
    New-Shortcut (Join-Path $startup 'Vienna Crawler Worker.lnk') 'wscript.exe' ('"' + (Join-Path $Dir 'worker\start-worker.vbs') + '"') $Dir 'Starts the Vienna Crawler Worker at login'
    $menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Vienna Crawler Worker'
    New-Item -ItemType Directory -Force -Path $menu | Out-Null
    New-Shortcut (Join-Path $menu 'Start Vienna Crawler Worker.lnk') (Join-Path $Dir 'Start Vienna Crawler Worker.bat') '' $Dir 'Start the worker'
    New-Shortcut (Join-Path $menu 'Stop Vienna Crawler Worker.lnk') (Join-Path $Dir 'Stop Vienna Crawler Worker.bat') '' $Dir 'Stop the worker'
    New-Shortcut (Join-Path $menu 'Uninstall Vienna Crawlers.lnk') (Join-Path $Dir 'Uninstall Vienna Crawlers.bat') '' $Dir 'Remove everything'
  }
  $statusFile = Join-Path $Dir 'status.json'
  Remove-Item $statusFile -Force -ErrorAction SilentlyContinue
  Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + (Join-Path $Dir 'worker\start-worker.vbs') + '"')

  $connected = $false
  for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 2
    if (Test-Path $statusFile) {
      try { $st = Get-Content $statusFile -Raw | ConvertFrom-Json } catch { $st = $null }
      if ($st -and $st.state -eq 'connected') { $connected = $true; break }
      if ($st -and $st.state -eq 'revoked') { throw 'Vienna does not recognise this setup file any more. Download a new one from the Vienna app.' }
    }
    Write-Host -NoNewline '.'
  }
  Write-Host ''

  Say ''
  if ($connected) {
    Say '  ============================================================' 'Green'
    Say '    EVERYTHING IS READY.' 'Green'
    Say '    The crawlers can now run on this computer.' 'Green'
    Say '  ============================================================' 'Green'
    Say '  Go back to the Vienna app - it now shows this computer as ready.'
    Say '  The worker runs quietly in the background and starts by itself'
    Say '  whenever you log in. Keep this computer on while crawls run.'
    Show-Box "Everything is ready.`r`n`r`nThe Vienna crawlers can now run on this computer. Go back to the Vienna app - it shows this computer as ready.`r`n`r`nThe worker runs quietly in the background and starts by itself when you log in." 'Vienna Crawler Setup' 'Information'
  } else {
    Say '  ============================================================' 'Yellow'
    Say '    Installed, but Vienna has not answered yet.' 'Yellow'
    Say '  ============================================================' 'Yellow'
    Say '  Check that this computer is online. The worker keeps retrying by'
    Say '  itself; the Vienna app will show it as ready once it connects.'
    Show-Box "The crawlers are installed, but this computer could not reach Vienna yet.`r`n`r`nCheck your internet connection. The worker keeps trying by itself, and the Vienna app will show this computer as ready once it connects." 'Vienna Crawler Setup' 'Warning'
  }
  if (-not $Test) { Read-Host '  Press Enter to close this window' }
  exit 0
}
catch {
  Say ''
  Say '  ============================================================' 'Red'
  Say '    Setup could not finish.' 'Red'
  Say '  ============================================================' 'Red'
  Say ("  " + $_.Exception.Message) 'Red'
  Say ("  Details were saved to: $LogFile")
  Say '  Please send a screenshot of this window (or that file) to whoever sent you the setup file.'
  Show-Box ("Setup could not finish.`r`n`r`n" + $_.Exception.Message + "`r`n`r`nDetails: $LogFile") 'Vienna Crawler Setup' 'Error'
  if (-not $Test) { Read-Host '  Press Enter to close this window' }
  exit 1
}
