using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using TKCode.Arc;
using ArcButton = TKCode.Arc.Button;

namespace DshDoraemon.Companion;

public sealed class PickerWindow : PopupWindow
{
    private readonly HashSet<PickerItem> _selection = [];
    private readonly ArcButton _confirm;

    public bool Selected { get; private set; }

    public PickerWindow(string title, IReadOnlyList<PickerItem> items, int anchorX, int anchorY)
    {
        Width = 480;
        Title = title;
        ConfigurePopup();

        var content = new StackPanel { Spacing = 9 };
        var header = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*") };
        var back = new ArcButton
        {
            Content = "返回", Variant = ButtonVariant.Ghost, Padding = new Thickness(10, 5),
        };
        back.Click += (_, _) => Close();
        header.Children.Add(back);
        var heading = new TextBlock
        {
            Text = title, FontSize = 16, FontWeight = FontWeight.SemiBold,
            Foreground = PrimaryText, VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(12, 0, 0, 0),
        };
        Grid.SetColumn(heading, 1);
        header.Children.Add(heading);
        content.Children.Add(header);
        content.Children.Add(new TextBlock
        {
            Text = "点击可勾选多项，确认后一次添加；不会覆盖已有授权。",
            FontSize = 11, Foreground = SecondaryText, Margin = new Thickness(2, 0, 2, 2),
        });

        var list = new StackPanel { Spacing = 4 };
        foreach (var item in items)
        {
            list.Children.Add(ItemButton(item));
        }
        if (items.Count == 0)
        {
            list.Children.Add(new TextBlock
            {
                Text = "没有可显示的项目", Foreground = SecondaryText, Margin = new Thickness(8),
            });
        }
        content.Children.Add(new ScrollViewer
        {
            Content = list, MaxHeight = 430,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        });

        var footer = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        var cancel = new ArcButton
        {
            Content = "取消", Variant = ButtonVariant.Ghost,
            HorizontalAlignment = HorizontalAlignment.Left, Padding = new Thickness(12, 7),
        };
        cancel.Click += (_, _) => Close();
        footer.Children.Add(cancel);
        _confirm = new ArcButton
        {
            Content = "添加所选 (0)", Variant = ButtonVariant.Primary, IsEnabled = false,
            Padding = new Thickness(16, 8),
        };
        Grid.SetColumn(_confirm, 1);
        _confirm.Click += (_, _) => Confirm(items);
        footer.Children.Add(_confirm);
        content.Children.Add(footer);

        Content = Panel(content);
        Opened += (_, _) => PlaceNear(anchorX, anchorY);
    }

    private ArcButton ItemButton(PickerItem item)
    {
        var row = new Grid
        {
            Width = 420,
            ColumnDefinitions = new ColumnDefinitions("26,36,*,Auto"),
            HorizontalAlignment = HorizontalAlignment.Left,
        };
        var check = new TextBlock
        {
            Text = "", FontSize = 16, Foreground = PrimaryText,
            VerticalAlignment = VerticalAlignment.Center,
        };
        row.Children.Add(check);
        var icon = IconLoader.ForItem(item);
        Control iconControl = new Border
        {
            Width = 26, Height = 26, CornerRadius = new CornerRadius(6),
            Background = new SolidColorBrush(Color.Parse("#35353B")),
            Child = icon is null
                ? new TextBlock
                {
                    Text = string.IsNullOrEmpty(item.Title) ? "?" : item.Title[..1],
                    HorizontalAlignment = HorizontalAlignment.Center,
                    VerticalAlignment = VerticalAlignment.Center,
                    Foreground = SecondaryText,
                }
                : new Image { Source = icon, Width = 26, Height = 26 },
            VerticalAlignment = VerticalAlignment.Center,
        };
        Grid.SetColumn(iconControl, 1);
        row.Children.Add(iconControl);

        var text = new StackPanel
        {
            HorizontalAlignment = HorizontalAlignment.Stretch,
            Children =
            {
                new TextBlock
                {
                    Text = item.Title, FontSize = 13, Foreground = PrimaryText,
                    TextTrimming = TextTrimming.CharacterEllipsis,
                },
                new TextBlock
                {
                    Text = item.Subtitle, FontSize = 10, Foreground = SecondaryText,
                    TextTrimming = TextTrimming.CharacterEllipsis,
                    Margin = new Thickness(0, 2, 0, 0),
                },
            },
        };
        Grid.SetColumn(text, 2);
        row.Children.Add(text);

        var button = new ArcButton
        {
            Content = row, Variant = ButtonVariant.Ghost, Height = 54,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Left,
            Padding = new Thickness(10, 6),
        };
        button.Click += (_, _) =>
        {
            if (!_selection.Remove(item)) _selection.Add(item);
            check.Text = _selection.Contains(item) ? "✓" : "";
            button.Variant = _selection.Contains(item) ? ButtonVariant.Secondary : ButtonVariant.Ghost;
            _confirm.Content = $"添加所选 ({_selection.Count})";
            _confirm.IsEnabled = _selection.Count > 0;
        };
        return button;
    }

    private void Confirm(IReadOnlyList<PickerItem> items)
    {
        if (_selection.Count == 0) return;
        Selected = true;
        Program.FinishBatch(items[0].Kind, _selection.ToList());
    }
}
