using System;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;

namespace MFAAvalonia.Services;

/// <summary>Keep provenance in the data, and show players the useful facts first.</summary>
public static class GameDatabasePresentation
{
    private static readonly Regex InlineStat = new(@"(?<label>[^\s:：+,，;；]+增幅（比例）|魂击率|命中|暴击|闪避|魔抗|韧性|破魔)(?<separator>\s*[:：+]\s*)(?<value>[+-]?\d+(?:\.\d+)?%?(?:\s*[;；～~\-]\s*\d+(?:\.\d+)?%?)*)",RegexOptions.Compiled);
    private static bool IsPercentage(string label) => label is "命中" or "暴击" or "闪避" or "魔抗" or "韧性" or "破魔" or "魂击率"
        || label.EndsWith("（比例）",StringComparison.Ordinal);

    public static GameFact[] Facts(GameDatabaseEntry entry) => entry.Facts
        .Where(fact => fact.Label is not ("编号" or "类型") && !string.IsNullOrWhiteSpace(fact.Value))
        .Select(fact => fact with { Label = fact.Label.Contains("冲突") ? "用途（资料参考）" : fact.Label switch
        {
            "获得方法" or "获取方法" or "获得方式" => "获取方式", "作用" => "用途",
            "出没点" or "出没地点" => "出现地点", _ => fact.Label
        }, Value = IsPercentage(fact.Label) ? Stat(fact) : FormatText(fact.Value.Trim()) })
        .DistinctBy(fact => (fact.Label,fact.Value)).OrderBy(fact => fact.Label is "获取方式" or "出现地点" or "制作方法" or "合成方法" ? 0 : 1).ToArray();

    public static string FormatText(string value) => InlineStat.Replace(value, match =>
        match.Groups["label"].Value.Replace("（比例）","") + match.Groups["separator"].Value
        + Stat(new(match.Groups["label"].Value,match.Groups["value"].Value)));

    public static string Stat(GameFact fact)
    {
        var parts = Regex.Split(fact.Value.Trim(),@"\s*[;；～~]\s*|(?<=[\d%])-\s*(?=\d)");
        var percentage = IsPercentage(fact.Label);
        return string.Join(" ～ ", parts.Select(part =>
            decimal.TryParse(part.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out var value)
                ? (percentage ? (value * 100).ToString("0.##", CultureInfo.InvariantCulture) + "%"
                              : value.ToString("#,0.##", CultureInfo.InvariantCulture)) : part));
    }

    public static string VariantTitle(GameConfigVariant variant) =>
        (variant.Locations.Length > 0 ? string.Join("、", variant.Locations) : "属性参考")
        + (string.IsNullOrWhiteSpace(variant.Level) ? "" : " · " + variant.Level + " 级");
}
