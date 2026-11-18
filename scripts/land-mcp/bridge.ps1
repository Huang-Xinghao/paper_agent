$ErrorActionPreference='Stop'
$mutex=New-Object Threading.Mutex($false,'Local\LANDMCPBridge')
if(-not $mutex.WaitOne(0)){exit}
$server=$null;$tunnel=$null
$sshArgs='-NT -i C:\Tools\land-mcp\id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o "ProxyCommand=C:/Tools/cloudflared.exe access ssh --hostname %h" -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -R 127.0.0.1:18766:127.0.0.1:8766 xinghaohuang@ssh.zhigroup.net'
try {
 while($true){
  if($null -eq $server -or $server.HasExited){
   $server=Start-Process -FilePath 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File C:\Tools\land-mcp\server.ps1' -WindowStyle Hidden -PassThru -RedirectStandardOutput 'C:\Tools\land-mcp\server.log' -RedirectStandardError 'C:\Tools\land-mcp\server-error.log'
  }
  if($null -eq $tunnel -or $tunnel.HasExited){
   $tunnel=Start-Process -FilePath 'C:\Windows\System32\OpenSSH\ssh.exe' -ArgumentList $sshArgs -WindowStyle Hidden -PassThru -RedirectStandardOutput 'C:\Tools\land-mcp\tunnel.log' -RedirectStandardError 'C:\Tools\land-mcp\tunnel-error.log'
  }
  Start-Sleep -Seconds 10
 }
} finally {
 foreach($proc in @($server,$tunnel)){if($null -ne $proc -and -not $proc.HasExited){$proc.Kill()}}
 $mutex.ReleaseMutex();$mutex.Dispose()
}
