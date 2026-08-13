$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $Desktop 'RipFoundry.lnk'
$PortableExe = Join-Path $ProjectDir 'RipFoundry.exe'
$BuiltExe = Join-Path $ProjectDir 'dist\RipFoundry\RipFoundry.exe'
if (Test-Path -LiteralPath $PortableExe) {
    $Target = $PortableExe
}
elseif (Test-Path -LiteralPath $BuiltExe) {
    $Target = $BuiltExe
}
else {
    throw 'RipFoundry.exe was not found. Keep Install-Shortcut.ps1 beside the portable EXE or build it first with Build-EXE.ps1.'
}
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Target
$Shortcut.WorkingDirectory = $ProjectDir
$Shortcut.Description = 'Rip DVDs and manage Jellyfin movie versions'
$Shortcut.IconLocation = "$Target,0"
$Shortcut.Save()
Write-Host "Created: $ShortcutPath"
