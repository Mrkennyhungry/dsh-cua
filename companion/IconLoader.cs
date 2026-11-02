using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using AvaloniaBitmap = Avalonia.Media.Imaging.Bitmap;

namespace DshDoraemon.Companion;

internal static class IconLoader
{
    private const uint ShgfiIcon = 0x000000100;
    private const uint ShgfiSmallIcon = 0x000000001;
    private static readonly Dictionary<string, AvaloniaBitmap?> Cache = new(StringComparer.OrdinalIgnoreCase);

    public static AvaloniaBitmap? ForItem(PickerItem item)
    {
        var path = item.Path;
        if (string.IsNullOrWhiteSpace(path) && item.Pid > 0)
        {
            try
            {
                path = Process.GetProcessById(item.Pid).MainModule?.FileName ?? "";
            }
            catch
            {
                // The process may have exited between enumeration and display.
            }
        }
        if (string.IsNullOrWhiteSpace(path))
        {
            return null;
        }
        lock (Cache)
        {
            if (Cache.TryGetValue(path, out var cached))
            {
                return cached;
            }
            var bitmap = Extract(path);
            Cache[path] = bitmap;
            return bitmap;
        }
    }

    private static AvaloniaBitmap? Extract(string path)
    {
        var info = new SHFILEINFO();
        var result = SHGetFileInfo(path, 0, ref info, (uint)Marshal.SizeOf(info),
                                   ShgfiIcon | ShgfiSmallIcon);
        if (result == IntPtr.Zero || info.hIcon == IntPtr.Zero)
        {
            return null;
        }
        try
        {
            using var icon = (Icon)Icon.FromHandle(info.hIcon).Clone();
            using var image = icon.ToBitmap();
            using var stream = new MemoryStream();
            image.Save(stream, ImageFormat.Png);
            stream.Position = 0;
            return new AvaloniaBitmap(stream);
        }
        catch
        {
            return null;
        }
        finally
        {
            DestroyIcon(info.hIcon);
        }
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr SHGetFileInfo(string path, uint attributes, ref SHFILEINFO info,
                                               uint size, uint flags);

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DestroyIcon(IntPtr handle);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct SHFILEINFO
    {
        public IntPtr hIcon;
        public int iIcon;
        public uint attributes;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string displayName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 80)] public string typeName;
    }
}
