using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace MFAAvalonia.Services;

public sealed record GameFact(string Label, string Value);
public sealed record GameSkill(string Name, string Description);
public sealed record GameReference(string Title, string Url, string Kind, string Date);
public sealed record GameConfigVariant(string Id, string Level, string[] Locations, GameFact[] Stats);
public sealed record GameWikiFact(string Label, string Value, string SourceUrl);

public sealed class GameDatabaseEntry
{
    public string Id { get; init; } = "";
    public string Name { get; init; } = "";
    public string Category { get; init; } = "";
    public string Subtype { get; init; } = "";
    public string? Icon { get; init; }
    public string Published { get; init; } = "";
    public GameFact[] Facts { get; init; } = [];
    public GameSkill[] Skills { get; init; } = [];
    public GameReference[] References { get; init; } = [];
    public GameConfigVariant[] ConfigVariants { get; init; } = [];
    public GameWikiFact[] WikiFacts { get; init; } = [];
    public string SearchText { get; private set; } = "";
    public void BuildSearchIndex() => SearchText = string.Join("\n", new[] { Name, Category, Subtype }
        .Concat(Facts.Select(f => f.Value)).Concat(Skills.Select(s => s.Name + " " + s.Description))
        .Concat(WikiFacts.Select(f => f.Value))
        .Concat(ConfigVariants.SelectMany(v => v.Locations)));
}

public sealed class GameDatabaseCatalog
{
    private sealed class Document
    {
        public int Version { get; init; }
        public string Game { get; init; } = "";
        public string CheckedOn { get; init; } = "";
        public GameDatabaseEntry[] Entries { get; init; } = [];
    }
    public string Root { get; }
    public string Game { get; }
    public string CheckedOn { get; }
    public IReadOnlyList<GameDatabaseEntry> Entries { get; }
    private GameDatabaseCatalog(string root, Document document)
    {
        Root = root; Game = document.Game; CheckedOn = document.CheckedOn;
        Entries = Array.AsReadOnly(document.Entries);
        foreach (var entry in Entries) entry.BuildSearchIndex();
    }
    public static GameDatabaseCatalog Load(string directory)
    {
        var root = Path.GetFullPath(directory);
        var file = Path.Combine(root, "catalog.json");
        var info = new FileInfo(file);
        if (!info.Exists) throw new FileNotFoundException("游戏数据库尚未安装，请更新完整客户端或项目资源。", file);
        if (info.Length > 32 * 1024 * 1024) throw new InvalidDataException("游戏数据库大小超出限制。");
        using var stream = File.OpenRead(file);
        var document = JsonSerializer.Deserialize<Document>(stream, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower })
            ?? throw new InvalidDataException("游戏数据库为空。");
        if (document.Version != 1 || document.Entries.Length is 0 or > 20000)
            throw new InvalidDataException("游戏数据库版本或条目数量无效。");
        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var entry in document.Entries)
        {
            if (string.IsNullOrWhiteSpace(entry.Id) || !ids.Add(entry.Id) || string.IsNullOrWhiteSpace(entry.Name)
                || entry.Category is not ("装备" or "道具" or "Boss") || entry.Facts == null || entry.Skills == null
                || entry.References == null || entry.ConfigVariants == null || entry.WikiFacts == null)
                throw new InvalidDataException("游戏数据库包含无效或重复的条目。");
            if (entry.Icon != null && !Regex.IsMatch(entry.Icon, @"\Aicons/[A-Za-z0-9_-]+\.png\z"))
                throw new InvalidDataException("游戏数据库图片路径无效。");
            if (entry.References.Any(r => !IsReferenceUrl(r.Url)))
                throw new InvalidDataException("游戏数据库来源链接无效。");
        }
        return new GameDatabaseCatalog(root, document);
    }
    public static bool IsReferenceUrl(string url) => Uri.TryCreate(url, UriKind.Absolute, out var uri)
        && uri.Scheme == Uri.UriSchemeHttps && string.IsNullOrEmpty(uri.UserInfo)
        && uri.Host is "news.4399.com" or "sda.4399.com" or "rotating-stars.github.io" or "github.com";

    public GameDatabaseEntry[] Search(string? query, string category = "全部", string subtype = "全部")
    {
        var terms = (query ?? "").Trim().Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        return Entries.Where(e => (category == "全部" || e.Category == category)
            && (subtype == "全部" || e.Subtype == subtype)
            && terms.All(t => e.SearchText.Contains(t, StringComparison.OrdinalIgnoreCase)))
            .OrderByDescending(e => string.Equals(e.Name, query?.Trim(), StringComparison.OrdinalIgnoreCase))
            .ThenBy(e => e.Name, StringComparer.Ordinal).ToArray();
    }
}
