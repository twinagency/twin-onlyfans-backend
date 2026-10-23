param([string]$BackendSha,[string]$DashboardSha)
$ErrorActionPreference = 'Stop'
if ($BackendSha -notmatch '^[0-9a-f]{40}$' -or $DashboardSha -notmatch '^[0-9a-f]{40}$') { throw 'Exact source SHAs required' }
function Assert-Native([string]$Step) { if ($LASTEXITCODE -ne 0) { throw "$Step failed: $LASTEXITCODE" } }
$BackendRepo = 'C:\Users\conta\codex-work\agency-launch-backend-20261022'
$DashboardRepo = 'C:\Users\conta\codex-work\agency-launch-dashboard-20261022'
$ReleaseBranch = 'codex/onlyfans-final-release-20261023'
$TaskRoot = Join-Path $env:TEMP ('twin-final-release-' + $BackendSha)
New-Item -ItemType Directory -Force -Path $TaskRoot | Out-Null
$Proofs = @()
foreach ($Entry in @(@{name='backend';repo=$BackendRepo;url='https://github.com/jesse98/twin-onlyfans-backend.git';sha=$BackendSha;format='zip';filename='backend-source.zip'},@{name='dashboard';repo=$DashboardRepo;url='https://github.com/jesse98/twin-dashboard.git';sha=$DashboardSha;format='tar.gz';filename='dashboard-source.tar.gz'})) {
  git -C $Entry.repo fetch --no-tags $Entry.url $ReleaseBranch
  Assert-Native ($Entry.name + ' fetch')
  $Fetched = (git -C $Entry.repo rev-parse FETCH_HEAD).Trim()
  Assert-Native ($Entry.name + ' source proof')
  if ($Fetched -ne $Entry.sha) { throw ($Entry.name + ' branch changed before archive') }
  $Names = @(git -C $Entry.repo ls-tree -r --name-only $Entry.sha)
  Assert-Native ($Entry.name + ' tracked-path review')
  $Unsafe = @($Names | Where-Object { $_ -match '(^|/)(node_modules|\.git|fan[-_]?data)/' -or ($_ -match '(^|/)logs?/' -and $_ -notmatch '\.(ts|tsx)$') -or $_ -match '\.(log|sqlite|sqlite3|db)$' -or ($_ -match '(^|/)\.env($|\.)' -and $_ -notmatch '(example|sample|template)$') })
  if ($Unsafe.Count -ne 0) { throw 'Archive contains a forbidden private environment or dependency path' }
  $Archive = Join-Path $TaskRoot $Entry.filename
  git -C $Entry.repo archive ("--format=" + $Entry.format) ("--output=" + $Archive) $Entry.sha
  Assert-Native ($Entry.name + ' exact tracked source archive')
  $Proofs += @{repository=$Entry.name;sha=$Entry.sha;tree=(git -C $Entry.repo rev-parse ($Entry.sha + '^{tree}')).Trim();archive=$Archive;sha256=(Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLowerInvariant();bytes=(Get-Item $Archive).Length;trackedPaths=$Names.Count}
}
$Result = @{recordedAt=[DateTime]::UtcNow.ToString('o');sourceArchives=$Proofs;sourceExported=$false;externalBuildStarted=$false;productionChanged=$false}
[IO.File]::WriteAllText((Join-Path $TaskRoot 'archives-only-proof.json'),($Result | ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
$Result | ConvertTo-Json -Depth 6
