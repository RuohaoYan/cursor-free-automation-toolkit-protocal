# 创建 GitHub 仓库 cursor-free-automation-toolkit-protocal 并推送 main 分支
# 用法: 先执行 gh auth login，再运行 .\scripts\publish_github.ps1

$ErrorActionPreference = "Stop"
$RepoName = "cursor-free-automation-toolkit-protocal"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "==> 检查 gh 登录状态..."
gh auth status
if ($LASTEXITCODE -ne 0) {
    Write-Host "请先运行: gh auth login"
    exit 1
}

$Owner = (gh api user -q .login).Trim()
$RemoteUrl = "https://github.com/$Owner/$RepoName.git"
Write-Host "==> 目标仓库: $RemoteUrl"

Write-Host "==> 创建 GitHub 仓库（若已存在则跳过）..."
gh repo view "$Owner/$RepoName" 2>$null
if ($LASTEXITCODE -ne 0) {
    gh repo create $RepoName `
        --public `
        --description "Cursor protocol registration automation with Kookeey proxy chain and CF bypass" `
        --source=. `
        --remote=origin `
        --push
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "==> 已创建并推送: $RemoteUrl"
    exit 0
}

Write-Host "==> 仓库已存在，更新 remote 并推送..."
if (git remote get-url origin 2>$null) {
    git remote set-url origin $RemoteUrl
} else {
    git remote add origin $RemoteUrl
}
git push -u origin main
Write-Host "==> 推送完成: $RemoteUrl"
