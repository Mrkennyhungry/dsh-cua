using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Media;

namespace DshDoraemon.Companion;

public abstract class PopupWindow : Window
{
    protected static readonly IBrush PanelBrush = new SolidColorBrush(Color.Parse("#F21C1C20"));
    protected static readonly IBrush PanelBorderBrush = new SolidColorBrush(Color.Parse("#38383D"));
    protected static readonly IBrush PrimaryText = Brushes.White;
    protected static readonly IBrush SecondaryText = new SolidColorBrush(Color.Parse("#A6A6AD"));

    protected void ConfigurePopup()
    {
        Topmost = true;
        ShowActivated = true;
        ShowInTaskbar = false;
        CanResize = false;
        WindowDecorations = WindowDecorations.None;
        WindowStartupLocation = WindowStartupLocation.Manual;
        SizeToContent = SizeToContent.Height;
        MaxHeight = 680;
        Background = PanelBrush;
        KeyDown += (_, e) =>
        {
            if (e.Key == Key.Escape)
            {
                Close();
            }
        };
    }

    protected void PlaceNear(int x, int y)
    {
        var screen = Screens.ScreenFromPoint(new PixelPoint(x, y)) ?? Screens.Primary;
        if (screen is null)
        {
            Position = new PixelPoint(x, y);
            return;
        }
        var area = screen.WorkingArea;
        // Window.Position and WorkingArea are physical pixels, while Bounds and
        // Width are logical. Convert with the monitor scale before clamping or a
        // 560-wide picker at 150% DPI still extends 280px past the right edge.
        var scaling = screen.Scaling;
        var logicalWidth = Bounds.Width > 0 ? Bounds.Width : Width;
        var logicalHeight = Bounds.Height > 0 ? Bounds.Height : MaxHeight;
        var width = (int)Math.Min(logicalWidth * scaling, area.Width - 16);
        var height = (int)Math.Min(logicalHeight * scaling, area.Height - 16);
        var px = Math.Clamp(x, area.X + 8, Math.Max(area.X + 8, area.Right - width - 8));
        var py = Math.Clamp(y, area.Y + 8, Math.Max(area.Y + 8, area.Bottom - height - 8));
        Position = new PixelPoint(px, py);
    }

    protected static Border Panel(Control child) => new()
    {
        Child = child,
        Padding = new Thickness(14),
        Background = PanelBrush,
        BorderBrush = PanelBorderBrush,
        BorderThickness = new Thickness(1),
        CornerRadius = new CornerRadius(14),
    };
}
