$ErrorActionPreference = "Stop"

git config core.hooksPath .githooks

Write-Host "Git hooks enabled from .githooks"
Write-Host "Before the first commit/push with PNG/JPG images, install Pillow if needed:"
Write-Host "  python -m pip install Pillow"
