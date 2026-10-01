param([switch]$Stop)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$phase5Project = Split-Path -Parent $PSScriptRoot
$phase5Cache = [IO.Path]::GetFullPath((Join-Path $phase5Project '.cache/phase5-es'))
if (-not $phase5Cache.StartsWith($phase5Project + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Elasticsearch cache must stay inside the project'
}
$phase5Version = '9.5.3'
$phase5Root = Join-Path $phase5Cache "elasticsearch-$phase5Version"
$phase5Listener = Get-NetTCPConnection -LocalPort 19200 -State Listen -ErrorAction SilentlyContinue
if ($phase5Listener) {
    $phase5Pid = $phase5Listener.OwningProcess | Select-Object -Unique
    $phase5Command = (Get-CimInstance Win32_Process -Filter "ProcessId=$phase5Pid").CommandLine
    if (-not $phase5Command.Contains($phase5Root, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Port 19200 belongs to another application; no process was stopped'
    }
    if ($Stop) {
        Stop-Process -Id $phase5Pid
        Write-Output 'Stopped the project-owned development Elasticsearch process; data preserved.'
    } else {
        Write-Output 'Project Elasticsearch is already listening on 127.0.0.1:19200.'
    }
    exit 0
}
if ($Stop) { exit 0 }
New-Item -ItemType Directory -Force -Path $phase5Cache | Out-Null

function Get-VerifiedArchive([string]$Url, [string]$Target) {
    if (-not (Test-Path -LiteralPath $Target)) {
        Invoke-WebRequest -Uri $Url -OutFile $Target
    }
    $phase5Checksum = ((Invoke-WebRequest -Uri ($Url + '.sha512')).Content -split '\s+')[0]
    if ((Get-FileHash -LiteralPath $Target -Algorithm SHA512).Hash -ne $phase5Checksum) {
        throw 'Official SHA-512 mismatch; archive was not executed or extracted'
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $phase5Root 'bin/elasticsearch.bat'))) {
    $phase5Archive = Join-Path $phase5Cache 'elasticsearch.zip'
    Get-VerifiedArchive "https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-$phase5Version-windows-x86_64.zip" $phase5Archive
    Expand-Archive -LiteralPath $phase5Archive -DestinationPath $phase5Cache
}
if (-not (Test-Path -LiteralPath (Join-Path $phase5Root 'plugins/analysis-smartcn/plugin-descriptor.properties'))) {
    $phase5Plugin = Join-Path $phase5Cache 'smartcn.zip'
    Get-VerifiedArchive "https://artifacts.elastic.co/downloads/elasticsearch-plugins/analysis-smartcn/analysis-smartcn-$phase5Version.zip" $phase5Plugin
    Expand-Archive -LiteralPath $phase5Plugin -DestinationPath (Join-Path $phase5Root 'plugins/analysis-smartcn')
}
@'
cluster.name: enterprise-support-phase5
node.name: phase5-dev
discovery.type: single-node
network.host: 127.0.0.1
http.port: 19200
transport.port: 19300
xpack.security.enabled: false
xpack.ml.enabled: false
cluster.routing.allocation.disk.watermark.low: 8gb
cluster.routing.allocation.disk.watermark.high: 6gb
cluster.routing.allocation.disk.watermark.flood_stage: 4gb
'@ | Set-Content -LiteralPath (Join-Path $phase5Root 'config/elasticsearch.yml') -Encoding utf8
$env:ES_JAVA_OPTS = '-Xms512m -Xmx512m'
$env:ES_JAVA_HOME = Join-Path $phase5Root 'jdk'
Start-Process -FilePath (Join-Path $phase5Root 'bin/elasticsearch.bat') -WorkingDirectory $phase5Root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $phase5Cache 'stdout.log') -RedirectStandardError (Join-Path $phase5Cache 'stderr.log')
Write-Output 'Started local development Elasticsearch. Logs are under .cache/phase5-es; check http://127.0.0.1:19200/_cluster/health after startup.'
