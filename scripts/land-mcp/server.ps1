param([int]$Port=8766)
$ErrorActionPreference='Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class LandNative {
 [DllImport(@"D:\LAND\安装\LAND\LANDCex_x64.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int GetDllVersion();
 [DllImport(@"D:\LAND\安装\LAND\LANDCex_x64.dll", CallingConvention=CallingConvention.Cdecl)] public static extern IntPtr LoadChlSnapshot(out int count, int mode);
 [DllImport(@"D:\LAND\安装\LAND\LANDCex_x64.dll", CallingConvention=CallingConvention.Cdecl)] public static extern void FreeChlSnapshot(IntPtr snapshot);
 [DllImport(@"D:\LAND\安装\LAND\LANDCex_x64.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int ChannelFromChlSnapshot(IntPtr snapshot, int index, out int box, out int channel);
}
'@
$token=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'land-mcp.token')).Trim()
function Snapshot {
 $count=0
 $handle=[LandNative]::LoadChlSnapshot([ref]$count,0)
 if($handle -eq [IntPtr]::Zero){throw 'LAND channel snapshot unavailable'}
 try{
  if($count -lt 0 -or $count -gt 2048){throw 'Invalid channel count'}
  $items=@(for($i=0;$i -lt $count;$i++){
   $box=0;$channel=0
   $ok=[LandNative]::ChannelFromChlSnapshot($handle,$i,[ref]$box,[ref]$channel)
   if($ok -ne 1){throw 'Channel enumeration failed'}
   [ordered]@{channel_id="local/LAND/windows/$($box+1)/$($channel+1)";box=$box+1;channel=$channel+1;native_box=$box;native_channel=$channel}
  })
  return ,$items
 }finally{[LandNative]::FreeChlSnapshot($handle)}
}
$toolDefinitions=@(
 @{name='land_health';description='Read local LAND DLL health and discovered channel inventory. Discovery does not establish device online or running state.';inputSchema=@{type='object';properties=@{};additionalProperties=$false};annotations=@{readOnlyHint=$true;destructiveHint=$false}},
 @{name='land_list_channels';description='List LAND channels. Display box/channel numbers are native zero-based identifiers plus one; native identifiers are included.';inputSchema=@{type='object';properties=@{};additionalProperties=$false};annotations=@{readOnlyHint=$true;destructiveHint=$false}},
 @{name='land_get_status';description='Return discovered channel identifiers with unknown operational status. Live status is not available through this verified interface.';inputSchema=@{type='object';properties=@{channel_ids=@{type='array';maxItems=128;items=@{type='string'}}};required=@('channel_ids');additionalProperties=$false};annotations=@{readOnlyHint=$true;destructiveHint=$false}},
 @{name='land_get_measurements';description='Report measurement availability. Voltage, current, capacity and energy are unavailable, never inferred or fabricated.';inputSchema=@{type='object';properties=@{channel_ids=@{type='array';maxItems=128;items=@{type='string'}}};required=@('channel_ids');additionalProperties=$false};annotations=@{readOnlyHint=$true;destructiveHint=$false}}
)
$listener=New-Object Net.HttpListener
$listener.Prefixes.Add("http://127.0.0.1:$Port/")
$listener.Start()
Write-Output "LAND MCP listening on loopback port $Port"
try{
 while($listener.IsListening){
  $ctx=$listener.GetContext()
  $response=$ctx.Response
  try{
   if($ctx.Request.Url.AbsolutePath -ne '/mcp'){$response.StatusCode=404;continue}
   if($ctx.Request.Headers['Authorization'] -cne ('Bearer '+$token)){$response.StatusCode=401;continue}
   if($ctx.Request.HttpMethod -eq 'DELETE'){$response.StatusCode=204;continue}
   if($ctx.Request.HttpMethod -ne 'POST'){$response.StatusCode=405;continue}
   if($ctx.Request.ContentLength64 -gt 65536){$response.StatusCode=413;continue}
   $reader=New-Object IO.StreamReader($ctx.Request.InputStream,[Text.Encoding]::UTF8)
   $request=$reader.ReadToEnd() | ConvertFrom-Json
   if(-not ($request.PSObject.Properties.Name -contains 'id')){$response.StatusCode=202;continue}
   $answer=@{jsonrpc='2.0';id=$request.id}
   switch($request.method){
    'initialize' {$answer.result=@{protocolVersion='2025-03-26';capabilities=@{tools=@{listChanged=$false}};serverInfo=@{name='land-readonly-mcp';version='1.0.0'};instructions='Read-only LAND channel discovery. Operational status and measurements remain unknown. No device control tools.'}}
    'ping' {$answer.result=@{}}
    'tools/list' {$answer.result=@{tools=$toolDefinitions}}
    'tools/call' {
     try{
      $name=$request.params.name
      if($name -notin @($toolDefinitions | ForEach-Object {$_.name})){throw 'Unknown tool'}
      $channels=Snapshot
      $now=[DateTime]::UtcNow.ToString('o')
      $payload=[ordered]@{observed_at=$now;source='LANDCex_x64.dll';discovery_only=$true;status_available=$false;measurements_available=$false}
      if($name -eq 'land_health'){
       $payload.reachable=$true
       $payload.dll_version=('0x'+[LandNative]::GetDllVersion().ToString('X8'))
       $payload.channels=@($channels)
       $payload.channel_count=$channels.Count
      }elseif($name -eq 'land_list_channels'){$payload.channels=@($channels)}
      else{
       $ids=@($request.params.arguments.channel_ids)
       if($ids.Count -gt 128){throw 'Maximum 128 channel IDs'}
       $known=@{};foreach($c in $channels){$known[$c.channel_id]=$c}
       $payload.channels=@(foreach($id in $ids){
        if(-not $known.ContainsKey([string]$id)){throw 'Unknown channel ID'}
        if($name -eq 'land_get_status'){@{channel_id=$id;status='unknown';stale=$true;reason='live_status_not_verified'}}
        else{@{channel_id=$id;stale=$true;voltage_v=$null;current_a=$null;capacity_ah=$null;energy_wh=$null;reason='live_measurements_not_verified'}}
       })
      }
      $answer.result=@{content=@(@{type='text';text=($payload | ConvertTo-Json -Depth 15 -Compress)});structuredContent=$payload;isError=$false}
     }catch{$answer.result=@{content=@(@{type='text';text=('LAND query failed: '+$_.Exception.Message)});isError=$true}}
    }
    default {$answer.error=@{code=-32601;message='Method not found'}}
   }
   $bytes=[Text.Encoding]::UTF8.GetBytes(($answer | ConvertTo-Json -Depth 22 -Compress))
   $response.ContentType='application/json; charset=utf-8'
   $response.ContentLength64=$bytes.Length
   $response.OutputStream.Write($bytes,0,$bytes.Length)
  }catch{$response.StatusCode=500}
  finally{$response.Close()}
 }
}finally{$listener.Stop();$listener.Close()}
