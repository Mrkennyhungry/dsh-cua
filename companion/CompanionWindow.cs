using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using TKCode.Arc;
using ArcButton = TKCode.Arc.Button;

namespace DshDoraemon.Companion;

public sealed class CompanionWindow : PopupWindow
{
    private readonly CompanionRequest _request;

    public CompanionWindow(CompanionRequest request)
    {
        _request = request;
        Width = 380;
        Title = "Doraemon 操作范围";
        ConfigurePopup();

        var content = new StackPanel { Spacing = 10 };
        content.Children.Add(new TextBlock
        {
            Text = "添加 AI 可操作范围",
            FontSize = 18,
            FontWeight = FontWeight.SemiBold,
            Foreground = PrimaryText,
            Margin = new Thickness(2, 2, 2, 0),
        });
        content.Children.Add(new TextBlock
        {
            Text = "可添加多个窗口或应用；新窗口与弹窗跟随所属应用",
            FontSize = 12,
            Foreground = SecondaryText,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(2, 0, 2, 8),
        });
        AddOption(content, "添加当前已打开的窗口",
                  $"{request.Windows.Count} 个窗口 · 可多选",
                  () => OpenPicker("window"), ButtonVariant.Secondary);
        AddOption(content, "添加已安装的应用",
                  $"{request.Apps.Count} 个开始菜单应用 · 可多选并启动",
                  () => OpenPicker("app"), ButtonVariant.Secondary);
        AddOption(content, "点选并添加窗口",
                  "Ctrl+Alt+W · 在屏幕上直接点选目标",
                  () => Program.Finish("action", "pick"), ButtonVariant.Ghost);
        AddOption(content, request.Stopped ? "解除急停" : "急停",
                  request.Stopped ? "恢复输入与会话操作" : "Ctrl+Alt+Q · 停止输入与当前会话",
                  () => Program.Finish("action", request.Stopped ? "resume" : "stop"),
                  request.Stopped ? ButtonVariant.Secondary : ButtonVariant.Danger);

        if (!string.IsNullOrWhiteSpace(request.LockedLabel))
        {
            AddOption(content, "清空全部授权", request.LockedLabel,
                      () => Program.Finish("action", "unlock"), ButtonVariant.Ghost);
        }
        AddOption(content, "隐藏吉祥物", "Ctrl+Alt+M 可随时重新显示",
                  () => Program.Finish("action", "close"), ButtonVariant.Ghost);
        Content = Panel(content);
        Opened += (_, _) => PlaceNear(request.AnchorX, request.AnchorY);
        Closed += (_, _) => Program.Cancel();
    }

    private static void AddOption(StackPanel parent, string title, string subtitle,
                                  Action action, ButtonVariant variant)
    {
        var button = new ArcButton
        {
            Variant = variant,
            Height = 56,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Left,
            Padding = new Thickness(16, 8),
            Content = new StackPanel
            {
                Children =
                {
                    new TextBlock { Text = title, FontSize = 14, Foreground = PrimaryText },
                    new TextBlock
                    {
                        Text = subtitle, FontSize = 11, Foreground = SecondaryText,
                        TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 3, 0, 0),
                    },
                },
            },
        };
        button.Click += (_, _) => action();
        parent.Children.Add(button);
    }

    private void OpenPicker(string kind)
    {
        var isWindow = kind == "window";
        var items = isWindow
            ? _request.Windows.Select(w => new PickerItem(
                "window", w.Title, w.Exe, w.Hwnd, w.Pid)).ToList()
            : _request.Apps.Select(a => new PickerItem(
                "app", a.Name, a.Path, Path: a.Path)).ToList();
        var picker = new PickerWindow(
            isWindow ? "添加已打开的窗口" : "添加要启动的应用", items,
            _request.AnchorX, _request.AnchorY);
        picker.Closed += (_, _) =>
        {
            if (!picker.Selected)
            {
                Show();
                Activate();
            }
        };
        Hide();
        picker.Show();
    }
}
