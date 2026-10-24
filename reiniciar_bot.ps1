# Reinicia el bot: mata su proceso y start_bridge.cmd lo relanza en 10 s.
# Lánzalo desde un proceso independiente (tarea programada o Win32_Process.Create) para no morir con el agente que lo pide.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match '\sbot\.py\s*$' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
