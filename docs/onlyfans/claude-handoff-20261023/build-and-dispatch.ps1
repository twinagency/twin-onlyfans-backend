param([string]$BackendSha,[string]$DashboardSha,[string]$ExpectedBackendArchiveSha256,[string]$ExpectedDashboardArchiveSha256)
$ErrorActionPreference = 'Stop'
if ($BackendSha -notmatch '^[0-9a-f]{40}$' -or $DashboardSha -notmatch '^[0-9a-f]{40}$') { throw 'Exact source SHAs required' }
if ($ExpectedBackendArchiveSha256 -notmatch '^[0-9a-f]{64}$' -or $ExpectedDashboardArchiveSha256 -notmatch '^[0-9a-f]{64}$') { throw 'Exact reviewed source archive hashes required' }
function Assert-Native([string]$Step) { if ($LASTEXITCODE -ne 0) { throw "$Step failed: $LASTEXITCODE" } }
$BackendRepo = 'C:\Users\conta\codex-work\agency-launch-backend-20261022'
$DashboardRepo = 'C:\Users\conta\codex-work\agency-launch-dashboard-20261022'
$ReleaseBranch = 'codex/onlyfans-final-release-20261023'
$TaskRoot = Join-Path $env:TEMP ('twin-final-release-' + $BackendSha)
New-Item -ItemType Directory -Force -Path $TaskRoot | Out-Null
git -C $BackendRepo fetch --no-tags https://github.com/jesse98/twin-onlyfans-backend.git $ReleaseBranch
Assert-Native 'Backend fetch'
$FetchedBackend = (git -C $BackendRepo rev-parse FETCH_HEAD).Trim()
Assert-Native 'Backend source proof'
if ($FetchedBackend -ne $BackendSha) { throw 'Backend branch moved before archive' }
git -C $DashboardRepo fetch --no-tags https://github.com/jesse98/twin-dashboard.git $ReleaseBranch
Assert-Native 'Dashboard fetch'
$FetchedDashboard = (git -C $DashboardRepo rev-parse FETCH_HEAD).Trim()
Assert-Native 'Dashboard source proof'
if ($FetchedDashboard -ne $DashboardSha) { throw 'Dashboard branch moved before archive' }
$BackendArchive = Join-Path $TaskRoot 'backend-source.zip'
$DashboardArchive = Join-Path $TaskRoot 'dashboard-source.tar.gz'
git -C $BackendRepo archive --format=zip --output=$BackendArchive $BackendSha
Assert-Native 'Backend archive'
git -C $DashboardRepo archive --format=tar.gz --output=$DashboardArchive $DashboardSha
Assert-Native 'Dashboard archive'
$BackendHash = (Get-FileHash -Algorithm SHA256 $BackendArchive).Hash.ToLowerInvariant()
$DashboardHash = (Get-FileHash -Algorithm SHA256 $DashboardArchive).Hash.ToLowerInvariant()
if ($BackendHash -ne $ExpectedBackendArchiveSha256 -or $DashboardHash -ne $ExpectedDashboardArchiveSha256) { throw 'Source archives differ from the reviewed local preparation' }
$Bucket = 'twin-worker-transfer-38e051d1'
$BackendKey = "onlyfans/releases/$BackendSha/source.zip"
$DashboardKey = "onlyfans/releases/$BackendSha/dashboard-$DashboardSha.tar.gz"
aws --profile tsocial --region ap-southeast-2 s3 cp $BackendArchive "s3://$Bucket/$BackendKey" --sse AES256 --only-show-errors
Assert-Native 'Backend source upload'
aws --profile tsocial --region ap-southeast-2 s3 cp $DashboardArchive "s3://$Bucket/$DashboardKey" --sse AES256 --only-show-errors
Assert-Native 'Dashboard source upload'
$SourceUrl = (aws --profile tsocial --region ap-southeast-2 s3 presign "s3://$Bucket/$DashboardKey" --expires-in 7200).Trim()
Assert-Native 'Paired source capability'
gh workflow run 388968454 --repo jesse98/twin-onlyfans-backend --ref $ReleaseBranch
Assert-Native 'Repository CI dispatch'
gh workflow run 393785294 --repo jesse98/twin-onlyfans-backend --ref $ReleaseBranch -f suite=combined -f dashboard_ref=$DashboardSha -f dashboard_archive_sha256=$DashboardHash -f dashboard_archive_url=$SourceUrl
Assert-Native 'Combined acceptance dispatch'
gh workflow run 393687470 --repo jesse98/twin-onlyfans-backend --ref $ReleaseBranch -f dashboard_ref=$DashboardSha -f dashboard_archive_sha256=$DashboardHash -f dashboard_archive_url=$SourceUrl
Assert-Native 'Commerce acceptance dispatch'
$ImageUri = '323149985112.dkr.ecr.ap-southeast-2.amazonaws.com/twin-onlyfans'
$BuildSpec = @"
version: 0.2
env:
  shell: bash
phases:
  pre_build:
    commands:
      - set -euo pipefail
      - aws ecr get-login-password --region ap-southeast-2 | docker login --username AWS --password-stdin 323149985112.dkr.ecr.ap-southeast-2.amazonaws.com
  build:
    commands:
      - docker build --pull --label org.opencontainers.image.revision=$BackendSha -f Dockerfile.onlyfans -t ${ImageUri}:$BackendSha .
  post_build:
    commands:
      - docker run --rm --entrypoint python ${ImageUri}:$BackendSha -m compileall -q /app/service
      - docker run --rm --entrypoint python ${ImageUri}:$BackendSha --version
      - docker push ${ImageUri}:$BackendSha
      - docker image inspect ${ImageUri}:$BackendSha --format '{{json .Config.Labels}}'
"@
$BuildRequest = @{projectName='twin-onlyfans-release';sourceLocationOverride="$Bucket/$BackendKey";buildspecOverride=$BuildSpec;environmentVariablesOverride=@(@{name='TWIN_RELEASE_SOURCE_SHA';value=$BackendSha;type='PLAINTEXT'},@{name='TWIN_RELEASE_SOURCE_SHA256';value=$BackendHash;type='PLAINTEXT'})}
$RequestPath = Join-Path $TaskRoot 'build-request.json'
[IO.File]::WriteAllText($RequestPath,($BuildRequest | ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
$BuildJson = aws --profile tsocial --region ap-southeast-2 codebuild start-build --cli-input-json "file://$RequestPath" --output json
Assert-Native 'Immutable image build'
$Build = $BuildJson | ConvertFrom-Json
$Result = @{backendSha=$BackendSha;dashboardSha=$DashboardSha;backendArchiveSha256=$BackendHash;dashboardArchiveSha256=$DashboardHash;backendSourceBytes=(Get-Item $BackendArchive).Length;dashboardSourceBytes=(Get-Item $DashboardArchive).Length;buildId=$Build.build.id;sourceBucket=$Bucket;sourceKey=$BackendKey;recordedAt=[DateTime]::UtcNow.ToString('o')}
$Result | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $TaskRoot 'source-and-build-proof.json') -Encoding UTF8
$Result | ConvertTo-Json -Depth 5
