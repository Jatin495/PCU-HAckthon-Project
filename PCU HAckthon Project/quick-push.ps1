param(
    [Parameter(Mandatory = $true)]
    [string]$Message,

    [string]$Branch = "main",
    [string]$Remote = "origin"
)

$ErrorActionPreference = "Stop"

# Stage all tracked and untracked changes.
git add -A

# If nothing is staged, exit gracefully.
$staged = git diff --cached --name-only
if (-not $staged) {
    Write-Output "No changes to commit."
    exit 0
}

git commit -m $Message
git push $Remote $Branch
