$env:Path = "C:\Program Files\Go\bin;C:\msys64\ucrt64\bin;C:\msys64\mingw64\bin;" + $env:Path
$env:CGO_ENABLED = "1"
Set-Location "$PSScriptRoot\whatsapp-bridge"
.\whatsapp-bridge.exe
