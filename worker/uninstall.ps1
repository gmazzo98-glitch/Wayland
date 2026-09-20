# Removes the Vienna Crawler Worker from this computer. Runs from a copy in %TEMP% so it can delete its own folder.
$Dir = $env:VDIR.TrimEnd('\')
Write-Host ''
Write-Host '  Removing the Vienna Crawler Worker from this computer...'
Get-Process node -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path.StartsWith($Dir, [StringComparison]::OrdinalIgnoreCase) } |
  ForEach-Object { & taskkill /F /T /PID $_.Id | Out-Null }
Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'Vienna Crawler Worker.lnk') -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath('Programs')) 'Vienna Crawler Worker') -Recurse -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Remove-Item $Dir -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path $Dir) {
  Write-Host "  Some files are still in use. Restart the computer and delete this folder by hand: $Dir"
} else {
  Write-Host '  Done. Everything was removed.'
}
Write-Host ''
Read-Host '  Press Enter to close'
