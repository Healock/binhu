function Resolve-BinhuWorkspaceLayout {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Root)

    $candidate = [System.IO.Path]::GetFullPath($Root)
    $workspaceRepo = Join-Path $candidate 'source'
    if ((Test-Path -LiteralPath (Join-Path $workspaceRepo 'desktop') -PathType Container) -and
        (Test-Path -LiteralPath (Join-Path $workspaceRepo 'VERSION') -PathType Leaf)) {
        return [pscustomobject]@{ DataRoot = $candidate; RepoRoot = $workspaceRepo }
    }
    if ((Test-Path -LiteralPath (Join-Path $candidate 'desktop') -PathType Container) -and
        (Test-Path -LiteralPath (Join-Path $candidate 'VERSION') -PathType Leaf)) {
        return [pscustomobject]@{ DataRoot = $candidate; RepoRoot = $candidate }
    }
    throw "Unable to locate the Binhu repository from: $candidate"
}
