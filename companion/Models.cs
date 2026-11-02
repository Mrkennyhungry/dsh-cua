using System.Text.Json.Serialization;

namespace DshDoraemon.Companion;

public sealed class CompanionRequest
{
    [JsonPropertyName("anchor_x")] public int AnchorX { get; set; }
    [JsonPropertyName("anchor_y")] public int AnchorY { get; set; }
    [JsonPropertyName("locked_label")] public string LockedLabel { get; set; } = "";
    [JsonPropertyName("stopped")] public bool Stopped { get; set; }
    [JsonPropertyName("windows")] public List<WindowItem> Windows { get; set; } = [];
    [JsonPropertyName("apps")] public List<AppItem> Apps { get; set; } = [];
}

public sealed class WindowItem
{
    [JsonPropertyName("hwnd")] public long Hwnd { get; set; }
    [JsonPropertyName("title")] public string Title { get; set; } = "";
    [JsonPropertyName("exe")] public string Exe { get; set; } = "";
    [JsonPropertyName("pid")] public int Pid { get; set; }
}

public sealed class AppItem
{
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("path")] public string Path { get; set; } = "";
}

public sealed record PickerItem(
    string Kind,
    string Title,
    string Subtitle,
    long Hwnd = 0,
    int Pid = 0,
    string Path = "");
