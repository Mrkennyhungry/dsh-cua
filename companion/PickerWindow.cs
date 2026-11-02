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
    public bool Selected { get; private set; }

    public PickerWindow(string title, IReadOnlyList<PickerItem> items, int anchorX, int anchorY)
    {
        Width = 560;
        Title = title;
        ConfigurePopup();

        var content = new StackPanel { Spacing = 10 };
        var header = new Grid
        {
            ColumnDefinitions = new ColumnDefinitions("Auto,*"),
            Margin = new Thickness(0, 0, 0, 4),
        };
        var back = new ArcButton
        {
            Content = "返回",
            Variant = ButtonVariant.Ghost,
            Padding = new Thickness(12, 6),
        };
        back.Click += (_, _) => Close();
        header.Children.Add(back);
        var heading = new TextBlock
        {
            Text = title,
            FontSize = 18,
            FontWeight = FontWeight.SemiBold,
            Foreground = PrimaryText,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(16, 0, 0, 0),
        };
        Grid.SetColumn(heading, 1);
        header.Children.Add(heading);
        content.Children.Add(header);

        var list = new StackPanel { Spacing = 5 };
        foreach (var item in items)
        {
            list.Children.Add(ItemButton(item));
        }
        if (items.Count == 0)
        {
            list.Children.Add(new TextBlock
            {
                Text = "没有可显示的项目",
                Foreground = SecondaryText,
                Margin = new Thickness(8),
            });
        }
        content.Children.Add(new ScrollViewer
        {
            Content = list,
            MaxHeight = 510,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
        });
        Content = Panel(content);
        Opened += (_, _) => PlaceNear(anchorX, anchorY);
    }

    private ArcButton ItemButton(PickerItem item)
    {
        var row = new Grid { ColumnDefinitions = new ColumnDefinitions("40,*") };
        var icon = IconLoader.ForItem(item);
        Control iconControl = icon is null
            ? new Border
            {
                Width = 28,
                Height = 28,
                CornerRadius = new CornerRadius(6),
                Background = new SolidColorBrush(Color.Parse("#35353B")),
                Child = new TextBlock
                {
                    Text = string.IsNullOrEmpty(item.Title) ? "?" : item.Title[..1],
                    HorizontalAlignment = HorizontalAlignment.Center,
                    VerticalAlignment = VerticalAlignment.Center,
                    Foreground = SecondaryText,
                },
            }
            : new Image { Source = icon, Width = 28, Height = 28 };
        iconControl.VerticalAlignment = VerticalAlignment.Center;
        row.Children.Add(iconControl);

        var text = new StackPanel
        {
            Children =
            {
                new TextBlock
                {
                    Text = item.Title, FontSize = 14, Foreground = PrimaryText,
                    TextTrimming = TextTrimming.CharacterEllipsis,
                },
                new TextBlock
                {
                    Text = item.Subtitle, FontSize = 11, Foreground = SecondaryText,
                    TextTrimming = TextTrimming.CharacterEllipsis,
                    Margin = new Thickness(0, 3, 0, 0),
                },
            },
        };
        Grid.SetColumn(text, 1);
        row.Children.Add(text);

        var button = new ArcButton
        {
            Content = row,
            Variant = ButtonVariant.Ghost,
            Height = 60,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Left,
            Padding = new Thickness(12, 7),
        };
        button.Click += (_, _) =>
        {
            Selected = true;
            Program.Finish(item.Kind, hwnd: item.Hwnd == 0 ? null : item.Hwnd,
                           path: string.IsNullOrEmpty(item.Path) ? null : item.Path);
        };
        return button;
    }
}
