'Capture a screen, window, image file or clipboard image for Claude. Use PowerShell and System.Drawing; shrink the longest side to at most 1568 pixels and send JPEG. Only a temporary file is written and promptly deleted. window selects the topmost non-MYN window using PrintWindow, screen selects the monitor under the pointer, and all captures all monitors. Return the viewed window and image dimensions as metadata.'

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

MAX_EDGE = 1568           # longest side sent to Claude
MAX_FILE_BYTES = 20 * 1024 * 1024
SEND_AS_IS_BYTES = 3_500_000  # smaller jpg/png/gif/webp files go as they are (API limit 5 MB)
TARGETS = ("window", "screen", "all")
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".gif": "image/gif", ".webp": "image/webp", ".bmp": None}


class VisionError(Exception):
    """A user-facing reason the picture could not be taken (Chinese)."""


def image_block(data, media_type="image/jpeg"):
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.b64encode(data).decode("ascii")}}


def _sniff(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_SHARED = r'''
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
function Save-Small([System.Drawing.Bitmap]$bmp, [string]$out, [int]$edge) {
  $w = $bmp.Width; $h = $bmp.Height
  $scale = [Math]::Min(1.0, $edge / [double][Math]::Max($w, $h))
  $nw = [Math]::Max(1, [int]($w * $scale)); $nh = [Math]::Max(1, [int]($h * $scale))
  $small = New-Object System.Drawing.Bitmap -ArgumentList @($nw, $nh)
  $g = [System.Drawing.Graphics]::FromImage($small)
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.DrawImage($bmp, 0, 0, $nw, $nh); $g.Dispose()
  $codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
  $params = New-Object System.Drawing.Imaging.EncoderParameters 1
  $params.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter -ArgumentList @([System.Drawing.Imaging.Encoder]::Quality, [long]82)
  $small.Save($out, $codec, $params); $small.Dispose()
  return @($w, $h, $nw, $nh)
}
'''

_WINDOWS = r'''
Add-Type -TypeDefinition @"
using System; using System.Text; using System.Runtime.InteropServices;
public class MynEyes {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int a, out int v, int s);
  public static IntPtr Found = IntPtr.Zero; public static string Title = "";
  public static void Pick(uint[] skip) {
    Found = IntPtr.Zero;
    EnumWindows(delegate (IntPtr h, IntPtr l) {
      if (!IsWindowVisible(h) || IsIconic(h)) return true;
      if ((GetWindowLong(h, -20) & 0x80) != 0) return true;          // tool windows
      int cloaked = 0; DwmGetWindowAttribute(h, 14, out cloaked, 4);
      if (cloaked != 0) return true;                                  // hidden by Windows
      uint pid; GetWindowThreadProcessId(h, out pid);
      foreach (uint s in skip) if (s == pid) return true;              // MYN's own windows
      RECT r; GetWindowRect(h, out r);
      if (r.R - r.L < 120 || r.B - r.T < 80) return true;
      var text = new StringBuilder(512); GetWindowText(h, text, 512);
      string t = text.ToString();
      if (t.Length == 0 || t == "Program Manager") return true;
      Found = h; Title = t; return false;                              // top-most of the rest
    }, IntPtr.Zero);
  }
}
"@
[MynEyes]::SetProcessDPIAware() | Out-Null
'''


class WindowsCamera:
    """Takes the pictures on Windows (replaced by a fake in tests)."""

    def __init__(self, skip_pids=()):
        self.skip_pids = [int(pid) for pid in skip_pids if pid]

    def _run(self, body, timeout=25):
        handle, out = tempfile.mkstemp(prefix="myn-look-", suffix=".jpg")
        os.close(handle)
        script = _SHARED + body.replace("__OUT__", out.replace("'", "''"))
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
                 "-EncodedCommand", encoded], capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            lines = [line for line in (result.stdout or "").strip().splitlines() if line.strip()]
            info = json.loads(lines[-1]) if lines else {}
            if result.returncode != 0 or not isinstance(info, dict):
                raise VisionError('Screen capture failed (PowerShell error).')
            if info.get("error"):
                raise VisionError(str(info["error"]))
            data = Path(out).read_bytes()
            if not data:
                raise VisionError('Screen capture failed (no image returned).')
            return data, info
        except subprocess.TimeoutExpired as error:
            raise VisionError('Screen capture timed out.') from error
        except (ValueError, OSError) as error:
            raise VisionError('Screen capture failed (%s).' % type(error).__name__) from error
        finally:
            try:
                os.remove(out)
            except OSError:
                pass

    def capture(self, target):
        skip = ",".join(str(pid) for pid in self.skip_pids) or "0"
        if target == "window":
            body = _WINDOWS + r'''
[MynEyes]::Pick([uint32[]]@(%s))
$h = [MynEyes]::Found
if ($h -eq [IntPtr]::Zero) { Write-Output '{"error":"No viewable window found."}'; exit 0 }
$r = New-Object MynEyes+RECT; [MynEyes]::GetWindowRect($h, [ref]$r) | Out-Null
$w = [int]($r.R - $r.L); $hh = [int]($r.B - $r.T)
$bmp = New-Object System.Drawing.Bitmap -ArgumentList @($w, $hh)
$g = [System.Drawing.Graphics]::FromImage($bmp); $dc = $g.GetHdc()
$ok = [MynEyes]::PrintWindow($h, $dc, 2); $g.ReleaseHdc($dc); $g.Dispose()
if (-not $ok) { $g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($r.L, $r.T, 0, 0, $bmp.Size); $g.Dispose() }
$s = Save-Small $bmp '__OUT__' %d; $bmp.Dispose()
$o = @{ what = 'window'; title = [MynEyes]::Title; width = $s[0]; height = $s[1]; sent = @($s[2], $s[3]) }
Write-Output ($o | ConvertTo-Json -Compress)
''' % (skip, MAX_EDGE)
        else:
            area = ("[System.Windows.Forms.SystemInformation]::VirtualScreen" if target == "all" else
                    "[System.Windows.Forms.Screen]::FromPoint([System.Windows.Forms.Cursor]::Position).Bounds")
            body = _WINDOWS + r'''
$b = %s
$bmp = New-Object System.Drawing.Bitmap -ArgumentList @([int]$b.Width, [int]$b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size); $g.Dispose()
$s = Save-Small $bmp '__OUT__' %d; $bmp.Dispose()
$o = @{ what = '%s'; title = ''; width = $s[0]; height = $s[1]; sent = @($s[2], $s[3]) }
Write-Output ($o | ConvertTo-Json -Compress)
''' % (area, MAX_EDGE, target)
        return self._run(body)

    def clipboard(self):
        return self._run(r'''
$img = [System.Windows.Forms.Clipboard]::GetImage()
if ($img -eq $null) { Write-Output '{"error":"No image in the clipboard."}'; exit 0 }
$bmp = New-Object System.Drawing.Bitmap -ArgumentList @(,$img)
$s = Save-Small $bmp '__OUT__' %d; $bmp.Dispose()
$o = @{ what = 'clipboard'; width = $s[0]; height = $s[1]; sent = @($s[2], $s[3]) }
Write-Output ($o | ConvertTo-Json -Compress)
''' % MAX_EDGE)

    def shrink(self, path):
        literal = str(path).replace("'", "''")
        return self._run(r'''
$bmp = New-Object System.Drawing.Bitmap -ArgumentList @(,[string]'%s')
$s = Save-Small $bmp '__OUT__' %d; $bmp.Dispose()
$o = @{ what = 'file'; width = $s[0]; height = $s[1]; sent = @($s[2], $s[3]) }
Write-Output ($o | ConvertTo-Json -Compress)
''' % (literal, MAX_EDGE))


def look(camera, target="window"):
    """(image block, info) for the screen / a window."""
    if target not in TARGETS:
        raise VisionError('Capture target must be window, screen or all.')
    data, info = camera.capture(target)
    return image_block(data), info


def from_clipboard(camera):
    data, info = camera.clipboard()
    return image_block(data), info


def from_file(camera, path):
    """(image block, info) for an image file; large ones are shrunk first."""
    path = Path(path)
    kind = IMAGE_TYPES.get(path.suffix.casefold(), "unknown")
    if kind == "unknown":
        raise VisionError('Only jpg, png, gif, webp and bmp images are supported.')
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise VisionError('Image exceeds 20 MB.')
    if kind is not None and size <= SEND_AS_IS_BYTES:
        data = path.read_bytes()
        media = _sniff(data)
        if media:
            return image_block(data, media), {"what": "file", "name": path.name, "bytes": size}
    data, info = camera.shrink(path)  # big, bmp, or not what its name says
    info["name"] = path.name
    return image_block(data), info
