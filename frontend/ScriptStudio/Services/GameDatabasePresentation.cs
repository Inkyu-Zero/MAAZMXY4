using System;
using System.Globalization;
using System.Linq;

namespace MFAAvalonia.Services;

/// <summary>Keep provenance in the data, and show players the useful facts first.</summary>
public static class GameDatabasePresentation
{
    public static GameFact[] Facts(GameDatabaseEntry entry) => entry.Facts
        .Where(fact => fact.Label is not ("编号" or "类型") && !string.IsNullOrWhiteSpace(fact.Value))
        .Select(fact => new GameFact(fact.Label.Contains("冲突") ? "用途（资料参考）" : fact.Label switch
        {
            "获得方法" or "获取方法" => "获取方式", "出没点" or "出没地点" => "出现地点", _ => fact.Label
        }, fact.Value.Trim()))
        .Distinct().OrderBy(fact => fact.Label is "获取方式" or "出现地点" or "制作方法" or "合成方法" ? 0 : 1).ToArray();

    public static string Stat(GameFact fact)
    {
        var parts = fact.Value.Split(';', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries);
        var percentage = fact.Label is "命中" or "暴击" or "闪避" or "魔抗" or "韧性";
        return string.Join(" ～ ", parts.Select(part =>
            decimal.TryParse(part, NumberStyles.Float, CultureInfo.InvariantCulture, out var value)
                ? (percentage ? (value * 100).ToString("0.##", CultureInfo.InvariantCulture) + "%"
                              : value.ToString("#,0.##", CultureInfo.InvariantCulture)) : part));
    }

    public static string VariantTitle(GameConfigVariant variant) =>
        (variant.Locations.Length > 0 ? string.Join("、", variant.Locations) : "属性参考")
        + (string.IsNullOrWhiteSpace(variant.Level) ? "" : " · " + variant.Level + " 级");
}
