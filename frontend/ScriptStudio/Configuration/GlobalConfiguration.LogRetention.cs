using System;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace MFAAvalonia.Configuration;

public static partial class GlobalConfiguration
{
    private const string LogRetentionKey = "LogRetention.Enabled";

    // Read before ConfigurationManager.Initialize: the logger starts earlier.
    public static bool GetLogRetentionEnabled()
    {
        lock (_fileLock)
        {
            var path = ConfigFilePath;
            var config = ReadRetentionConfiguration(File.Exists(path) ? File.ReadAllBytes(path) : null);
            if (!config.TryGetPropertyValue(LogRetentionKey, out var node)) return true;
            if (node is JsonValue value)
            {
                if (value.TryGetValue<bool>(out var enabled)) return enabled;
                if (value.TryGetValue<string>(out var text) && bool.TryParse(text, out enabled)) return enabled;
            }
            throw new InvalidDataException("日志滚动设置无效，请检查全局配置。");
        }
    }

    public static void SetLogRetentionEnabled(bool enabled)
    {
        // Share the host's write lock; retain unrelated and unknown fields.
        lock (_fileLock)
        {
            var path = ConfigFilePath;
            var original = File.Exists(path) ? File.ReadAllBytes(path) : null;
            var config = ReadRetentionConfiguration(original);
            config[LogRetentionKey] = enabled.ToString(); // Host settings use strings.
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var suffix = Guid.NewGuid().ToString("N");
            var temporary = path + "." + suffix + ".tmp";
            try
            {
                using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    var bytes = Encoding.UTF8.GetBytes(config.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
                    stream.Write(bytes); stream.Flush(true);
                }
                if (original == null) File.Move(temporary, path);
                else
                {
                    if (!original.AsSpan().SequenceEqual(File.ReadAllBytes(path)))
                        throw new IOException("全局配置已变化，请重试保存日志滚动设置。");
                    // Atomic replacement keeps the previous bytes in a unique
                    // local backup and never overwrites an existing backup.
                    File.Replace(temporary, path, path + ".log-retention-" + suffix + ".bak");
                }
            }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }
    }

    private static JsonObject ReadRetentionConfiguration(byte[]? bytes)
    {
        if (bytes == null) return new JsonObject();
        return JsonNode.Parse(Encoding.UTF8.GetString(bytes).TrimStart('\uFEFF'), documentOptions:
            new JsonDocumentOptions { AllowTrailingCommas = true, CommentHandling = JsonCommentHandling.Skip }) as JsonObject
            ?? throw new InvalidDataException("全局配置不是 JSON 对象，未修改文件。");
    }
}
