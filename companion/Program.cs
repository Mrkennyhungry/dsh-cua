using System.Text;
using System.Text.Json;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Threading;
using TKCode.Arc;

namespace DshDoraemon.Companion;

internal static class Program
{
    private static bool _server;
    private static bool _written;
    private static Timer? _parentTimer;

    public static CompanionRequest Request { get; private set; } = new();

    [STAThread]
    public static int Main(string[] args)
    {
        // The kernel writes JSON as UTF-8. Console.In defaults to the Windows
        // ANSI code page, which corrupts Chinese window titles before parsing.
        Console.InputEncoding = new UTF8Encoding(false);
        Console.OutputEncoding = new UTF8Encoding(false);
        _server = args.Length == 2 && args[0] == "--server";
        if (!_server && args.Length != 1)
        {
            return 2;
        }
        try
        {
            if (!_server)
            {
                Request = JsonSerializer.Deserialize<CompanionRequest>(
                    File.ReadAllText(args[0]),
                    new JsonSerializerOptions { PropertyNameCaseInsensitive = true }) ?? new();
            }
            BuildAvaloniaApp().StartWithClassicDesktopLifetime(args);
            return 0;
        }
        catch (Exception ex)
        {
            TryWrite(new Dictionary<string, object?>
            {
                ["kind"] = "error", ["error"] = ex.ToString(),
            });
            return 1;
        }
    }

    public static AppBuilder BuildAvaloniaApp() =>
        AppBuilder.Configure<App>()
                  .UsePlatformDetect()
                  .WithInterFont()
                  .UseArc(arc => arc
                      .UseArcControls()
                      .WithTheme(t => t.Appearance(ArcAppearance.Dark)));

    public static void Finish(string kind, string? action = null, long? hwnd = null, string? path = null)
    {
        if (!TryWrite(new Dictionary<string, object?>
        {
            ["kind"] = kind, ["action"] = action, ["hwnd"] = hwnd, ["path"] = path,
        }))
        {
            return;
        }
        CloseAll();
        if (!_server)
        {
            Shutdown();
        }
    }

    public static void Cancel()
    {
        if (!TryWrite(new Dictionary<string, object?> { ["kind"] = "cancel" }))
        {
            return;
        }
        if (!_server)
        {
            Shutdown();
        }
    }

    public static void BeginServer(int parentPid)
    {
        // A dead kernel must not leave an invisible UI process behind.
        _parentTimer = new Timer(_ =>
        {
            try
            {
                System.Diagnostics.Process.GetProcessById(parentPid);
            }
            catch
            {
                Dispatcher.UIThread.Post(Shutdown);
            }
        }, null, TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(5));
        Task.Run(() =>
        {
            string? line;
            while ((line = Console.In.ReadLine()) is not null)
            {
                if (string.IsNullOrWhiteSpace(line))
                {
                    continue;
                }
                try
                {
                    var request = JsonSerializer.Deserialize<CompanionRequest>(line);
                    if (request is not null)
                    {
                        Dispatcher.UIThread.Post(() =>
                        {
                            CloseAll();
                            _written = false;
                            new CompanionWindow(request).Show();
                        });
                    }
                }
                catch (Exception ex)
                {
                    TryWrite(new Dictionary<string, object?>
                    {
                        ["kind"] = "error", ["error"] = ex.ToString(),
                    });
                }
            }
            Dispatcher.UIThread.Post(Shutdown);
        });
    }

    private static void CloseAll()
    {
        if (Application.Current?.ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            foreach (var window in desktop.Windows.ToList())
            {
                window.Close();
            }
        }
    }

    private static void Shutdown()
    {
        if (Application.Current?.ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            desktop.Shutdown();
        }
    }

    private static bool TryWrite(Dictionary<string, object?> result)
    {
        if (_written)
        {
            return false;
        }
        _written = true;
        try
        {
            Console.Out.WriteLine(JsonSerializer.Serialize(result));
            Console.Out.Flush();
        }
        catch (IOException)
        {
            // The kernel may already have exited and closed our stdout pipe.
        }
        return true;
    }
}
