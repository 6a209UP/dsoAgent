[CmdletBinding()]
param(
    [string]$OutputDir,
    [string]$Version
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $repoRoot 'dist\1panel'
}
$outputFull = [System.IO.Path]::GetFullPath($OutputDir)
New-Item -ItemType Directory -Path $outputFull -Force | Out-Null

if ([string]::IsNullOrWhiteSpace($Version)) {
    $commit = (& git -C $repoRoot rev-parse --short=12 HEAD).Trim()
    if (-not $commit) {
        $commit = 'nogit'
    }
    $dirty = (& git -C $repoRoot status --porcelain).Count -gt 0
    $suffix = if ($dirty) { '-working' } else { '' }
    $Version = "$(Get-Date -Format 'yyyyMMdd')-$commit$suffix"
}

if ($Version -notmatch '^[0-9A-Za-z._-]+$') {
    throw 'Version may contain only letters, numbers, dot, underscore and hyphen.'
}

$stageRoot = Join-Path $outputFull ('.staging-' + [guid]::NewGuid().ToString('N'))
$backendRoot = Join-Path $stageRoot 'backend'
$frontendRoot = Join-Path $stageRoot 'frontend'
New-Item -ItemType Directory -Path $backendRoot -Force | Out-Null
New-Item -ItemType Directory -Path $frontendRoot -Force | Out-Null

function Copy-RelativeFile {
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$DestinationRoot
    )

    $relativeNative = $RelativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
    $source = Join-Path $repoRoot $relativeNative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Required source file is missing: $RelativePath"
    }
    $destination = Join-Path $DestinationRoot $relativeNative
    $parent = Split-Path -Parent $destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

function Convert-TextFileToLf {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $content = [System.IO.File]::ReadAllText($Path)
    $content = $content.Replace("`r`n", "`n").Replace("`r", "`n")
    [System.IO.File]::WriteAllText(
        $Path,
        $content,
        [System.Text.UTF8Encoding]::new($false)
    )
}

try {
    $rootFiles = @(
        'app.py',
        'config.py',
        'config-template.json',
        'pyproject.toml',
        'requirements.txt',
        'requirements-optional.txt',
        'run.sh',
        'LICENSE',
        'README.md'
    )
    $backendPrefixes = @(
        'agent/',
        'bridge/',
        'channel/',
        'cli/',
        'common/',
        'models/',
        'plugins/',
        'scripts/',
        'skills/',
        'translate/',
        'voice/',
        'docker/'
    )

    $tracked = @(& git -C $repoRoot ls-files)
    $untracked = @(& git -C $repoRoot ls-files --others --exclude-standard)
    $sourceFiles = @($tracked + $untracked | Sort-Object -Unique)

    foreach ($relative in $sourceFiles) {
        $path = $relative.Replace('\', '/')
        $include = $rootFiles -contains $path
        if (-not $include) {
            foreach ($prefix in $backendPrefixes) {
                if ($path.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
                    $include = $true
                    break
                }
            }
        }
        if (-not $include) {
            continue
        }

        if ($path -match '(^|/)(__pycache__|\.pytest_cache|node_modules)(/|$)' -or
            $path -match '\.(pyc|pyo|log)$') {
            continue
        }
        Copy-RelativeFile -RelativePath $path -DestinationRoot $backendRoot
    }

    Copy-Item -LiteralPath (Join-Path $repoRoot 'docker\Dockerfile.latest') `
        -Destination (Join-Path $backendRoot 'Dockerfile.1panel') -Force

    # The release package is generated on Windows but executed in Linux.
    # A CRLF shebang makes Linux report the misleading error
    # "exec /entrypoint.sh: no such file or directory" because it looks for
    # /bin/bash\r. Normalize every packaged shell script before creating ZIP.
    Get-ChildItem -LiteralPath $backendRoot -Recurse -File -Filter '*.sh' |
        ForEach-Object { Convert-TextFileToLf -Path $_.FullName }

    @'
__pycache__/
*.pyc
*.pyo
*.log
.git/
.github/
.pytest_cache/
config.json
*.zip
dist/
tmp/
'@ | Set-Content -LiteralPath (Join-Path $backendRoot '.dockerignore') -Encoding utf8 -NoNewline

    $deployDir = Join-Path $backendRoot 'deploy'
    New-Item -ItemType Directory -Path $deployDir -Force | Out-Null
    @'
{
  "cow_lang": "zh",
  "channel_type": "web",
  "web_host": "0.0.0.0",
  "web_port": 9899,
  "web_password": "REPLACE_WITH_A_LONG_RANDOM_PASSWORD",
  "agent": true,
  "agent_workspace": "/home/agent/cow/default",
  "self_evolution_enabled": false,
  "open_ai_api_base": "https://approved-model.example.com/v1",
  "open_ai_api_key": "REPLACE_WITH_MODEL_API_KEY",
  "model": "approved-model-name",
  "bot_type": "openai",
  "source2_integrations": []
}
'@ | Set-Content -LiteralPath (Join-Path $deployDir 'config.1panel.example.json') -Encoding utf8 -NoNewline

    @'
# Copy this file outside the extracted source tree before adding real values.
# Source2 secrets use tenant-specific names, for example:
# SOURCE2_1001_APP_SECRET=REPLACE_ME
# SOURCE2_1001_ADMIN_APP_SECRET=REPLACE_ME
# SOURCE2_1001_DATA_KEY_V1=REPLACE_ME
'@ | Set-Content -LiteralPath (Join-Path $deployDir 'cowagent.env.example') -Encoding utf8 -NoNewline

    @"
services:
  cowagent:
    image: zhuojian/cowagent:$Version
    container_name: zhuojian-cowagent
    restart: unless-stopped
    env_file:
      - /opt/zhuojian/secrets/cowagent.env
    ports:
      - "127.0.0.1:19899:9899"
    volumes:
      - type: bind
        source: /opt/zhuojian/cowagent/config.json
        target: /app/config.json
        read_only: true
        bind:
          create_host_path: false
          selinux: Z
      - type: bind
        source: /opt/zhuojian/cowagent/data
        target: /home/agent/cow
        bind:
          create_host_path: false
          selinux: Z
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - NET_RAW
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9899/api/health', timeout=5)"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 40s
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "5"
"@ | Set-Content -LiteralPath (Join-Path $deployDir 'docker-compose.1panel.yml') -Encoding utf8 -NoNewline

    $backendReadme = @'
# CowAgent 后端 1Panel 部署包

版本：__VERSION__

1. 在 1Panel“主机 → 文件”上传并解压本 ZIP，确认 `Dockerfile.1panel` 与 `app.py` 位于同一目录。
2. 进入“容器 → 镜像 → 构建镜像”：
   - 名称：`zhuojian/cowagent:__VERSION__`
   - Dockerfile：选择解压目录中的 `Dockerfile.1panel`
   - 构建参数：每行填写 `INSTALL_BROWSER=false`、`USE_CN_MIRROR=true`
3. 将 `deploy/config.1panel.example.json` 复制到 `/opt/zhuojian/cowagent/config.json`，替换示例值；将配置文件和 `/opt/zhuojian/cowagent/data` 的属主设置为镜像实际 UID/GID，配置文件权限设为 `0600`、数据目录设为 `0700`。
4. 如需 Source2，在源码目录外创建 `/opt/zhuojian/secrets/cowagent.env`，不要把真实密钥写回本解压目录。
5. 在“容器 → 编排模板”导入 `deploy/docker-compose.1panel.yml`，再从该模板创建 1Panel 编排。
6. 后端镜像已经包含完整 CowAgent Web 前端；OpenResty 将整个站点反向代理到 `127.0.0.1:19899`，浏览器访问 `/` 后由 CowAgent 跳转到 `/chat`，不需要另建静态前端站点。
7. CentOS Stream 10 保持 SELinux Enforcing；Compose 已为私有挂载配置 `selinux: Z`，不要删除该配置或执行 `setenforce 0`。
8. 保持 firewalld 启用，公网仅放行 80/443；19899 只绑定回环地址。
9. 如更新过部署包，必须使用新镜像标签并关闭构建缓存；旧镜像不会因覆盖 ZIP 自动更新。

健康检查：`http://127.0.0.1:19899/api/health`

完整说明参见 CowAgent 仓库：`docs/zh/development/cowagent-1panel-deployment-guide.md`。
'@
    $backendReadme.Replace('__VERSION__', $Version) |
        Set-Content -LiteralPath (Join-Path $backendRoot 'README-1Panel.md') -Encoding utf8 -NoNewline

    # Standalone Web frontend: chat.html becomes index.html and static becomes assets.
    Copy-Item -LiteralPath (Join-Path $repoRoot 'channel\web\static') `
        -Destination (Join-Path $frontendRoot 'assets') -Recurse -Force
    $frontendHtml = Get-Content -LiteralPath (Join-Path $repoRoot 'channel\web\chat.html') -Raw -Encoding utf8
    $frontendHtml = $frontendHtml.Replace('{{COW_DEFAULT_LANG}}', 'zh')
    Set-Content -LiteralPath (Join-Path $frontendRoot 'index.html') -Value $frontendHtml -Encoding utf8 -NoNewline

    @'
# Add these locations to the 1Panel OpenResty site configuration.
# Replace REPLACE_WITH_1PANEL_SITE_ROOT with the absolute site directory.

location = / {
    root REPLACE_WITH_1PANEL_SITE_ROOT;
    try_files /index.html =404;
}

location = /index.html {
    root REPLACE_WITH_1PANEL_SITE_ROOT;
    add_header Cache-Control "no-cache, no-store, must-revalidate" always;
}

location ^~ /assets/ {
    root REPLACE_WITH_1PANEL_SITE_ROOT;
    try_files $uri =404;
    expires 7d;
    add_header Cache-Control "public, max-age=604800";
}

# All non-static paths are CowAgent APIs, auth, uploads, SSE or previews.
location / {
    proxy_pass http://127.0.0.1:19899;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 300s;
    proxy_buffering off;
    proxy_cache off;
}
'@ | Set-Content -LiteralPath (Join-Path $frontendRoot '1panel-openresty.conf') -Encoding utf8 -NoNewline

$frontendReadme = @'
# CowAgent 前端 1Panel 静态站点包（可选）

版本：__VERSION__

默认部署不使用本包。后端镜像已经内置完整 Web 前端，按 CowAgent 1Panel 教程把整个域名反向代理到 `127.0.0.1:19899` 即可。本包只用于明确需要由 OpenResty 独立托管静态资源的高级部署。

1. 在 1Panel“网站”中创建 CowAgent 的静态站点并启用 HTTPS。
2. 上传本 ZIP 到站点根目录并解压；根目录应直接出现 `index.html` 和 `assets/`，不能多套一层目录。
3. 将 `1panel-openresty.conf` 中的 `REPLACE_WITH_1PANEL_SITE_ROOT` 替换成站点根目录。
4. 把其中的 location 配置合并到站点高级配置并重载 OpenResty。
5. 后端必须已运行在 `127.0.0.1:19899`。前后端必须使用同一域名，避免登录 Cookie 和跨域问题。

不要把 `config.json`、模型 Key、Source2 Secret 或患者数据上传到静态站点目录。
'@
    $frontendReadme.Replace('__VERSION__', $Version) |
        Set-Content -LiteralPath (Join-Path $frontendRoot 'README-1Panel.md') -Encoding utf8 -NoNewline

    $assetReferences = [regex]::Matches($frontendHtml, '(?:src|href)="(assets/[^"?]+)"') |
        ForEach-Object { $_.Groups[1].Value } |
        Sort-Object -Unique
    foreach ($assetReference in $assetReferences) {
        $assetPath = Join-Path $frontendRoot $assetReference.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
        if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf)) {
            throw "Frontend asset referenced by index.html is missing: $assetReference"
        }
    }

    $backendFileCount = (Get-ChildItem -LiteralPath $backendRoot -Recurse -File).Count
    $frontendFileCount = (Get-ChildItem -LiteralPath $frontendRoot -Recurse -File).Count
    if ($backendFileCount -lt 50) {
        throw "Backend package is unexpectedly small ($backendFileCount files)."
    }
    if ($frontendFileCount -lt 10) {
        throw "Frontend package is unexpectedly small ($frontendFileCount files)."
    }

    @"
package=cowagent-backend-1panel
version=$Version
files=$backendFileCount
contains_worktree_changes=true
secrets_included=false
"@ | Set-Content -LiteralPath (Join-Path $backendRoot 'PACKAGE-MANIFEST.txt') -Encoding utf8 -NoNewline
    @"
package=cowagent-frontend-1panel
version=$Version
files=$frontendFileCount
contains_worktree_changes=true
secrets_included=false
"@ | Set-Content -LiteralPath (Join-Path $frontendRoot 'PACKAGE-MANIFEST.txt') -Encoding utf8 -NoNewline

    $backendZip = Join-Path $outputFull "cowagent-backend-1panel-$Version.zip"
    $frontendZip = Join-Path $outputFull "cowagent-frontend-1panel-$Version.zip"
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $backendZip) { Remove-Item -LiteralPath $backendZip -Force }
    if (Test-Path -LiteralPath $frontendZip) { Remove-Item -LiteralPath $frontendZip -Force }
    [System.IO.Compression.ZipFile]::CreateFromDirectory($backendRoot, $backendZip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
    [System.IO.Compression.ZipFile]::CreateFromDirectory($frontendRoot, $frontendZip, [System.IO.Compression.CompressionLevel]::Optimal, $false)

    $backendHash = (Get-FileHash -LiteralPath $backendZip -Algorithm SHA256).Hash.ToLowerInvariant()
    $frontendHash = (Get-FileHash -LiteralPath $frontendZip -Algorithm SHA256).Hash.ToLowerInvariant()
    @(
        "$backendHash  $(Split-Path -Leaf $backendZip)",
        "$frontendHash  $(Split-Path -Leaf $frontendZip)"
    ) | Set-Content -LiteralPath (Join-Path $outputFull 'SHA256SUMS.txt') -Encoding ascii

    [pscustomobject]@{
        Version = $Version
        BackendZip = $backendZip
        BackendFiles = $backendFileCount
        BackendSha256 = $backendHash
        FrontendZip = $frontendZip
        FrontendFiles = $frontendFileCount
        FrontendSha256 = $frontendHash
    }
}
finally {
    if (Test-Path -LiteralPath $stageRoot) {
        $stageFull = [System.IO.Path]::GetFullPath($stageRoot)
        $outputPrefix = $outputFull.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
        if (-not $stageFull.StartsWith($outputPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove staging path outside output directory: $stageFull"
        }
        Remove-Item -LiteralPath $stageFull -Recurse -Force
    }
}
