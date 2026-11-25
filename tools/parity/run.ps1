# Runs tools/parity/compare.html in headless Edge (real GPU + H.264) and prints the results.
# Output PNGs (card shader / FlameRenderer / |diff| x8) land in -Out.
param([string]$Out = "$env:TEMP\fpcmp\out", [int]$Port = 8775, [int]$Width = 1000, [int]$Timeout = 240,
  [string]$Assets = "", [string]$Cases = "", [string[]]$EdgeArgs = @())
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
Remove-Item $Out -Recurse -Force -ErrorAction SilentlyContinue
$srv = Start-Process python -ArgumentList "$PSScriptRoot\collect.py", $Port, $Out -WorkingDirectory $repo -PassThru -WindowStyle Hidden
$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
$prof = Join-Path $env:TEMP "fpcmp-prof-$Port"
$q = "w=$Width"; if ($Assets) { $q += "&assets=$Assets" }; if ($Cases) { $q += "&cases=$Cases" }
$ea = @("--headless=new", "--user-data-dir=$prof", "--autoplay-policy=no-user-gesture-required",
  "--ignore-gpu-blocklist", "--window-size=1200,900") + $EdgeArgs + @("`"http://127.0.0.1:$Port/tools/parity/compare.html?$q`"")
$e = Start-Process $edge -PassThru -ArgumentList $ea
try {
  $deadline = (Get-Date).AddSeconds($Timeout)
  while ((Get-Date) -lt $deadline -and -not (Test-Path "$Out\results.json") -and -not (Test-Path "$Out\error.txt")) { Start-Sleep 2 }
} finally {
  foreach ($p in @($e, $srv)) { if ($p -and -not $p.HasExited) { $p.Kill($true) } }
}
if (Test-Path "$Out\error.txt") { Get-Content "$Out\error.txt"; exit 1 }
if (-not (Test-Path "$Out\results.json")) { "timeout"; exit 2 }
Get-Content "$Out\results.json" | ConvertFrom-Json | Format-Table -AutoSize
