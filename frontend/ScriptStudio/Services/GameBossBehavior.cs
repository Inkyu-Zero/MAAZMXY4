using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace MFAAvalonia.Services;

public sealed class GameBehaviorNode
{
    public string Title { get; init; } = "";
    public string Text { get; init; } = "";
    public string[] EvidenceIds { get; init; } = [];
    public GameBehaviorNode[] Children { get; init; } = [];
    public IEnumerable<GameBehaviorNode> Descendants() => new[] { this }.Concat(Children.SelectMany(n => n.Descendants()));
}

public sealed record GameBehaviorEvidence(string Id, string Title, string Version, string Locator,
    string Sha256, string Url, string SourceSha256);

public sealed class GameBossBehavior
{
    public string EntryId { get; init; } = "";
    public string[] MonsterIds { get; init; } = [];
    public string Scope { get; init; } = "";
    public string Version { get; init; } = "";
    public string Summary { get; init; } = "";
    public GameBehaviorNode[] Roots { get; init; } = [];
    public IReadOnlyDictionary<string, GameBehaviorEvidence> Evidence { get; private set; } = new Dictionary<string, GameBehaviorEvidence>();
    public IEnumerable<GameBehaviorNode> Nodes => Roots.SelectMany(n => n.Descendants());
    public IEnumerable<GameBehaviorEvidence> Sources => Nodes.SelectMany(n => n.EvidenceIds).Distinct().Select(id => Evidence[id]);

    private sealed class Document
    {
        public int Version { get; init; }
        public GameBossBehavior[] Profiles { get; init; } = [];
        public GameBehaviorEvidence[] Evidence { get; init; } = [];
    }

    public static void Attach(string directory, IReadOnlyList<GameDatabaseEntry> entries)
    {
        var path = Path.Combine(directory, "boss-behaviors.json");
        if (!File.Exists(path)) return; // Existing catalogue-only packages remain readable.
        if (new FileInfo(path).Length > 2 * 1024 * 1024) throw new InvalidDataException("Boss 行为资料过大。");
        using var stream = File.OpenRead(path);
        var doc = JsonSerializer.Deserialize<Document>(stream, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, MaxDepth = 32 })
            ?? throw new InvalidDataException("Boss 行为资料为空。");
        if (doc.Version != 1 || doc.Profiles == null || doc.Evidence == null || doc.Profiles.Length > 500 || doc.Evidence.Length > 4000)
            throw new InvalidDataException("Boss 行为资料版本或数量无效。");
        var evidence = new Dictionary<string, GameBehaviorEvidence>(StringComparer.Ordinal);
        foreach (var source in doc.Evidence)
        {
            if (source == null || string.IsNullOrWhiteSpace(source.Id) || !evidence.TryAdd(source.Id, source)
                || string.IsNullOrWhiteSpace(source.Title) || string.IsNullOrWhiteSpace(source.Version)
                || string.IsNullOrWhiteSpace(source.Locator) || !GameDatabaseCatalog.IsReferenceUrl(source.Url)
                || !Hash(source.Sha256) || !Hash(source.SourceSha256))
                throw new InvalidDataException("Boss 行为证据无效或重复。");
        }
        var byId = entries.ToDictionary(entry => entry.Id, StringComparer.Ordinal);
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var profile in doc.Profiles)
        {
            if (profile == null || string.IsNullOrWhiteSpace(profile.EntryId) || !seen.Add(profile.EntryId) || !byId.TryGetValue(profile.EntryId, out var entry)
                || entry.Category != "Boss" || profile.MonsterIds == null || profile.MonsterIds.Length == 0
                || profile.MonsterIds.Any(id => !entry.ConfigVariants.Any(v => v.Id == id))
                || string.IsNullOrWhiteSpace(profile.Scope) || string.IsNullOrWhiteSpace(profile.Version)
                || string.IsNullOrWhiteSpace(profile.Summary) || profile.Roots == null || profile.Roots.Length == 0)
                throw new InvalidDataException("Boss 行为条目或适用变体无效。");
            var count = 0;
            void Validate(GameBehaviorNode node, int depth)
            {
                if (node == null || depth > 6 || ++count > 100 || string.IsNullOrWhiteSpace(node.Title)
                    || node.Title.Length > 800 || node.Text == null || node.Text.Length > 1200
                    || node.Children == null || node.EvidenceIds == null || node.EvidenceIds.Length == 0
                    || node.EvidenceIds.Any(id => string.IsNullOrWhiteSpace(id) || !evidence.ContainsKey(id)))
                    throw new InvalidDataException("Boss 行为树节点或证据引用无效。");
                foreach (var child in node.Children) Validate(child, depth + 1);
            }
            foreach (var root in profile.Roots) Validate(root, 0);
            profile.Evidence = evidence;
            entry.Behavior = profile;
        }
    }
    private static bool Hash(string? value) => value != null && Regex.IsMatch(value, @"\A[a-f0-9]{64}\z");
}
