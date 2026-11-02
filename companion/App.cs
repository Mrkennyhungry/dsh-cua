using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;

namespace DshDoraemon.Companion;

public sealed class App : Application
{
    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            desktop.ShutdownMode = ShutdownMode.OnExplicitShutdown;
            // Server mode starts with no window: the Arc runtime is already warm
            // when the user clicks the mascot, and a window is created on demand.
            if (!Environment.GetCommandLineArgs().Contains("--server"))
            {
                desktop.MainWindow = new CompanionWindow(Program.Request);
            }
            desktop.Exit += (_, _) => Program.Cancel();
        }
        base.OnFrameworkInitializationCompleted();
        var args = Environment.GetCommandLineArgs();
        var serverIndex = Array.IndexOf(args, "--server");
        if (serverIndex >= 0 && serverIndex + 1 < args.Length)
        {
            // Only now is the Win32 dispatcher owned by the UI thread. Starting
            // the stdin listener earlier let a fast request claim it first.
            Program.BeginServer(int.Parse(args[serverIndex + 1]));
        }
    }
}
