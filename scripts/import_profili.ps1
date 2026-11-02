param(
  [string]$MdbPath = "",
  [string]$OutFile = ""
)
$ErrorActionPreference = "Stop"
if (-not $MdbPath) {
  $candidates = @(
    "<PROFILI_INSTALL>\InstLibraries\Profili.mdb",
    "C:\Program Files\ProfiliV2\InstLibraries\Profili.mdb",
    "C:\Program Files (x86)\ProfiliV2\InstLibraries\Profili.mdb"
  )
  $MdbPath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $MdbPath -or -not (Test-Path $MdbPath)) { throw "Profili.mdb not found; pass -MdbPath" }
if (-not $OutFile) {
  $base = Join-Path $env:LOCALAPPDATA "OpenVSP-R2\airfoils"
  New-Item -ItemType Directory -Force -Path $base | Out-Null
  $OutFile = Join-Path $base "profili_library.json"
} else {
  New-Item -ItemType Directory -Force -Path (Split-Path $OutFile) | Out-Null
}
$connStr = "Driver={Microsoft Access Driver (*.mdb, *.accdb)};Dbq=$MdbPath;"
$conn = New-Object System.Data.Odbc.OdbcConnection($connStr)
$conn.Open()
# metadata
$meta = @{}
$cmd = $conn.CreateCommand(); $cmd.CommandText = "SELECT Id, Nome, MaxWidth, MaxCamber, MaxCamberSuCorda FROM Profili"
$r = $cmd.ExecuteReader()
while ($r.Read()) {
  $id = [int]$r.GetValue(0)
  $meta[$id] = [pscustomobject]@{
    Name = [string]$r.GetValue(1)
    Thickness = [double]$r.GetValue(2)
    Camber = [double]$r.GetValue(3)
    CamberPos = [double]$r.GetValue(4)
  }
}
$r.Close()
# coordinates
$coords = @{}
$cmd = $conn.CreateCommand(); $cmd.CommandText = "SELECT Fk_Id_Profilo, FlagSopraSotto, X, Y FROM Coordinate ORDER BY Fk_Id_Profilo, FlagSopraSotto, X"
$r = $cmd.ExecuteReader()
while ($r.Read()) {
  $id = [int]$r.GetValue(0); $flag = [int]$r.GetValue(1); $x = [double]$r.GetValue(2); $y = [double]$r.GetValue(3)
  if (-not $coords.ContainsKey($id)) { $coords[$id] = New-Object System.Collections.ArrayList }
  [void]$coords[$id].Add([pscustomobject]@{ Flag=$flag; X=$x; Y=$y })
}
$r.Close(); $conn.Close()
$out = [System.IO.StreamWriter]::new($OutFile, $false, [System.Text.UTF8Encoding]::new($false))
try {
  $out.Write('{"format":1,"source":"Profili 2.x local database","mdb":"' + ($MdbPath -replace '\\','\\') + '","count":' + $coords.Count + ',"airfoils":[')
  $first = $true
  foreach ($id in ($coords.Keys | Sort-Object)) {
    $m = $meta[$id]; if (-not $m) { continue }
    $upper = @($coords[$id] | Where-Object { $_.Flag -eq 0 } | Sort-Object X)
    $lower = @($coords[$id] | Where-Object { $_.Flag -eq 1 } | Sort-Object X)
    if ($upper.Count -lt 5 -or $lower.Count -lt 5) { continue }
    if (-not $first) { $out.Write(',') }; $first = $false
    $name = $m.Name.Replace('\','\\').Replace('"','\"')
    $out.Write('{"id":' + $id + ',"name":"' + $name + '","thickness_pct":' + $m.Thickness.ToString('0.####',[Globalization.CultureInfo]::InvariantCulture) + ',"camber_pct":' + $m.Camber.ToString('0.####',[Globalization.CultureInfo]::InvariantCulture) + ',"camber_pos_pct":' + $m.CamberPos.ToString('0.####',[Globalization.CultureInfo]::InvariantCulture) + ',"upper":[')
    $ufirst = $true
    foreach ($p in $upper) { if (-not $ufirst) { $out.Write(',') }; $ufirst=$false; $out.Write('[' + ($p.X/100.0).ToString('0.#####',[Globalization.CultureInfo]::InvariantCulture) + ',' + ($p.Y/100.0).ToString('0.#####',[Globalization.CultureInfo]::InvariantCulture) + ']') }
    $out.Write('],"lower":[')
    $lfirst = $true
    foreach ($p in $lower) { if (-not $lfirst) { $out.Write(',') }; $lfirst=$false; $out.Write('[' + ($p.X/100.0).ToString('0.#####',[Globalization.CultureInfo]::InvariantCulture) + ',' + ($p.Y/100.0).ToString('0.#####',[Globalization.CultureInfo]::InvariantCulture) + ']') }
    $out.Write(']}')
  }
  $out.Write(']}')
} finally { $out.Close() }
Write-Host "Wrote $OutFile ($([math]::Round((Get-Item $OutFile).Length/1MB,2)) MB)"
