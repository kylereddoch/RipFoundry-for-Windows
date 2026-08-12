$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $Desktop 'RipFoundry.lnk'
$Target = Join-Path $ProjectDir 'Launch-RipFoundry.bat'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Target
$Shortcut.WorkingDirectory = $ProjectDir
$Shortcut.Description = 'Rip DVDs and manage Jellyfin movie versions'
$Icon = Join-Path $ProjectDir 'assets\RipFoundry.ico'
if (Test-Path -LiteralPath $Icon) {
    $Shortcut.IconLocation = "$Icon,0"
}
$Shortcut.Save()
Write-Host "Created: $ShortcutPath"
