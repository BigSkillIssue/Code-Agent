"""The PowerShell that runs inside a device session's Windows Sandbox (W62).

It publishes the product's Windows app (dotnet, from the read-only tools folder), starts it,
answers action 0 with the first screen, then takes the agent's actions from the job's control
folder one at a time (`req-<seq>.json`), does them on the sandbox's desktop and leaves the answer
(`res-<seq>.json`, and `screen-<seq>.png`) until a `stop` file appears; then the sandbox shuts
down. Clicks, keys and the wheel go through user32 and SendKeys; the screen's elements come from
UI Automation, limited to the app's own windows.
"""

SCRIPT = r"""$ErrorActionPreference = 'Stop'
$control = 'C:\job\.device'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing, UIAutomationClient, UIAutomationTypes
Add-Type @'
using System; using System.Runtime.InteropServices;
public static class ForgeInput {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")]
  public static extern void mouse_event(uint f, uint x, uint y, int d, UIntPtr e);
}
'@
[ForgeInput]::SetProcessDPIAware() | Out-Null
$app = $null

function Write-Answer($name, $answer) {
  $json = $answer | ConvertTo-Json -Depth 4 -Compress
  Set-Content -Encoding UTF8 -Path "$control\$name.tmp" -Value $json
  Move-Item -Force -Path "$control\$name.tmp" -Destination "$control\$name.json"
}

function Save-Screen($path) {
  $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
  $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose()
  return @($b.Width, $b.Height)
}

function App-Windows {
  $byApp = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $app.Id)
  return @([System.Windows.Automation.AutomationElement]::RootElement.FindAll(
    [System.Windows.Automation.TreeScope]::Children, $byApp))
}

function Elements {
  $found = New-Object System.Collections.ArrayList
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
  foreach ($window in App-Windows) {
    $stack = New-Object System.Collections.Stack
    $stack.Push($window)
    while ($stack.Count -gt 0 -and $found.Count -lt 500) {
      $e = $stack.Pop()
      $c = $e.Current
      $r = $c.BoundingRectangle
      if (($c.Name -or $c.AutomationId) -and -not $r.IsEmpty) {
        [void]$found.Add([pscustomobject]@{
          kind = ($c.ControlType.ProgrammaticName -replace '^ControlType\.', '')
          name = [string]$c.Name; id = [string]$c.AutomationId
          x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height })
      }
      $child = $walker.GetFirstChild($e)
      while ($child) { $stack.Push($child); $child = $walker.GetNextSibling($child) }
    }
  }
  return $found
}

function Find-Element($target) {
  $all = Elements
  $t = $target.ToLowerInvariant()
  $hit = $all | Where-Object {
    $_.name.ToLowerInvariant() -eq $t -or $_.id.ToLowerInvariant() -eq $t } | Select-Object -First 1
  if (-not $hit) {
    $hit = $all | Where-Object { $_.name.ToLowerInvariant().Contains($t) } | Select-Object -First 1
  }
  return $hit
}

function Click($x, $y) {
  [ForgeInput]::SetCursorPos([int]$x, [int]$y) | Out-Null
  [ForgeInput]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
  [ForgeInput]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
}

function Keys($text) { return ($text -replace '([+^%~(){}\[\]])', '{$1}') }

$job = Get-Content -Raw -Encoding UTF8 -Path "$control\device.json" | ConvertFrom-Json
$env:DOTNET_ROOT = 'C:\forge-tools\0'
$env:PATH = 'C:\forge-tools\0;' + $env:PATH
$env:NUGET_PACKAGES = 'C:\job\.nuget'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'
try {
  & dotnet publish "C:\job\src\$($job.project)" -c Release -o 'C:\job\publish' 2>&1 |
    Out-File -Encoding utf8 -FilePath "$control\publish.out"
  if ($LASTEXITCODE -ne 0) { throw 'the Windows app did not build' }
  $app = Start-Process -FilePath "C:\job\publish\$($job.exe)" -PassThru
  $deadline = (Get-Date).AddSeconds(90)
  while ((Get-Date) -lt $deadline -and (App-Windows).Count -eq 0) { Start-Sleep -Milliseconds 500 }
  if ((App-Windows).Count -eq 0) { throw 'the app showed no window' }
  Start-Sleep -Seconds 2
  $size = Save-Screen "$control\screen-0.png"
  Write-Answer 'res-0' @{ seq = 0; ok = $true; text = "$($job.exe) runs"
                          width = $size[0]; height = $size[1] }
} catch {
  $log = ''
  if (Test-Path "$control\publish.out") { $log = Get-Content -Raw -Path "$control\publish.out" }
  if ($log.Length -gt 3000) { $log = $log.Substring($log.Length - 3000) }
  Write-Answer 'res-0' @{ seq = 0; ok = $false; error = "$_"; text = $log }
  shutdown.exe /s /t 0
  exit
}

$scroll = @{ up = -600; down = 600; left = 600; right = -600 }
$keys = @{ back = '%{LEFT}'; home = '{HOME}'; enter = '{ENTER}'; tab = '{TAB}'; escape = '{ESC}';
           menu = '+{F10}'; delete = '{DEL}' }
while (-not (Test-Path "$control\stop")) {
  $req = Get-ChildItem -Path "$control\req-*.json" -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if (-not $req) { Start-Sleep -Milliseconds 200; continue }
  $a = Get-Content -Raw -Encoding UTF8 -Path $req.FullName | ConvertFrom-Json
  Remove-Item -Force -Path $req.FullName
  $answer = @{ seq = $a.seq; ok = $true; text = '' }
  try {
    switch ($a.kind) {
      'tap' {
        if ($null -ne $a.x) { Click $a.x $a.y; $answer.text = "clicked at $($a.x),$($a.y)" }
        else {
          $hit = Find-Element $a.target
          if (-not $hit) { throw "nothing on the screen is called '$($a.target)'" }
          Click ($hit.x + [int]($hit.w / 2)) ($hit.y + [int]($hit.h / 2))
          $answer.text = "clicked $($hit.kind) '$($hit.name)'"
        }
      }
      'type' {
        [System.Windows.Forms.SendKeys]::SendWait((Keys $a.text))
        if ($a.submit) { [System.Windows.Forms.SendKeys]::SendWait('{ENTER}') }
        $answer.text = 'typed'
      }
      'swipe' {
        $flag = 0x0800
        if ($a.direction -eq 'left' -or $a.direction -eq 'right') { $flag = 0x1000 }
        [ForgeInput]::mouse_event($flag, 0, 0, $scroll[$a.direction], [UIntPtr]::Zero)
        $answer.text = "scrolled ($($a.direction))"
      }
      'key' {
        [System.Windows.Forms.SendKeys]::SendWait($keys[$a.key])
        $answer.text = "pressed $($a.key)"
      }
      'read' {
        $lines = Elements | ForEach-Object {
          $id = ''; if ($_.id) { $id = " id=$($_.id)" }
          "$($_.kind) '$($_.name)'$id ($($_.x),$($_.y),$($_.w),$($_.h))" }
        $answer.text = ($lines -join "`n")
      }
    }
    if ($a.kind -ne 'read') {
      Start-Sleep -Milliseconds 800
      $size = Save-Screen "$control\screen-$($a.seq).png"
      $answer.width = $size[0]; $answer.height = $size[1]
    }
  } catch { $answer.ok = $false; $answer.error = "$_" }
  Write-Answer "res-$($a.seq)" $answer
}
shutdown.exe /s /t 0
"""
