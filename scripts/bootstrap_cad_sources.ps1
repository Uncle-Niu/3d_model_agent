param(
    [switch]$Update
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$targetRoot = Join-Path $repoRoot "data\cad_sources"

$repos = @(
    @{
        Name = "awesome-cadquery"
        Url = "https://github.com/CadQuery/awesome-cadquery.git"
    },
    @{
        Name = "cadquery-contrib"
        Url = "https://github.com/CadQuery/cadquery-contrib.git"
    },
    @{
        Name = "build123d"
        Url = "https://github.com/gumyr/build123d.git"
    },
    @{
        Name = "cadquery"
        Url = "https://github.com/CadQuery/cadquery.git"
    },
    @{
        Name = "cq_warehouse"
        Url = "https://github.com/gumyr/cq_warehouse.git"
    },
    @{
        Name = "cadquery-models"
        Url = "https://github.com/tanius/cadquery-models.git"
    }
)

New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null

foreach ($repo in $repos) {
    $dest = Join-Path $targetRoot $repo.Name
    if (Test-Path $dest) {
        Write-Host "Found $($repo.Name) at $dest"
        if ($Update) {
            Write-Host "Updating $($repo.Name)..."
            git -C $dest pull --ff-only
        }
        continue
    }

    Write-Host "Cloning $($repo.Name)..."
    git clone --depth 1 $repo.Url $dest
}

Write-Host "CAD source banks are ready in $targetRoot"
