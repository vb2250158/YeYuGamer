using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Web.Script.Serialization;

internal static class EndfieldProfileReplay
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

    public static int Main(string[] args)
    {
        if (args == null || args.Length != 2) return 64;
        string runnerPath = Path.GetFullPath(args[0]);
        string configPath = Path.GetFullPath(args[1]);
        Assembly runner = Assembly.LoadFrom(runnerPath);
        Type program = runner.GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        Type bindingType = program.GetNestedType("Binding", BindingFlags.NonPublic);
        MethodInfo apply = program.GetMethod("ApplyEndfieldDailyTaskProfile", BindingFlags.NonPublic | BindingFlags.Static);
        MethodInfo ensure = program.GetMethod("EnsureDailyConfigForInjection", BindingFlags.NonPublic | BindingFlags.Static);
        MethodInfo restore = program.GetMethod("RestoreDailyConfigSnapshot", BindingFlags.NonPublic | BindingFlags.Static);
        MethodInfo restoreOrPreserve = program.GetMethod("RestoreOrPreserveDailyConfig", BindingFlags.NonPublic | BindingFlags.Static);
        MethodInfo preservePending = program.GetMethod("PreserveDailyConfigForRecovery", BindingFlags.NonPublic | BindingFlags.Static);
        MethodInfo restorePending = program.GetMethod("RestorePendingDailyConfig", BindingFlags.NonPublic | BindingFlags.Static);
        if (bindingType == null || apply == null || ensure == null || restore == null || restoreOrPreserve == null || preservePending == null || restorePending == null) return 65;

        var original = new Dictionary<string, object> {
            { "⭐刷体力", true }, { "体力本", "旧副本" }, { "体力本奖励档位", "保持当前" },
            { "刷体力开始日期", "2026-01-01" }, { "刷本序列", new object[0] }, { "指定的队伍编号", "不换队伍" }
        };
        File.WriteAllText(configPath, Json.Serialize(original), new UTF8Encoding(false));
        Invoke(apply, bindingType, configPath, new object[] { "干员经验低阶", "超距辉映管" });
        IDictionary<string, object> actual = Json.DeserializeObject(File.ReadAllText(configPath, Encoding.UTF8)) as IDictionary<string, object>;
        if (actual == null || (actual["体力本"] as string) != "干员经验" ||
            (actual["体力本奖励档位"] as string) != "高阶" ||
            (actual["刷体力开始日期"] as string) != "2026-04-06" ||
            (actual["指定的队伍编号"] as string) != "2" ||
            !SequenceEquals(actual["刷本序列"] as object[], new[] { "干员经验低阶", "超距辉映管" }) ||
            !(actual["⭐刷体力"] is bool) || !(bool)actual["⭐刷体力"])
            return 66;
        byte[] originalBytes = File.ReadAllBytes(configPath);
        File.WriteAllText(configPath, "{}", new UTF8Encoding(false));
        InvokeStatic(restore, configPath, originalBytes, true);
        if (!File.ReadAllBytes(configPath).SequenceEqual(originalBytes)) return 67;

        File.WriteAllText(configPath, Json.Serialize(original), new UTF8Encoding(false));
        try { Invoke(apply, bindingType, configPath, new object[] { "超距辉映管高阶" }); }
        catch (InvalidOperationException) { goto missingConfig; }
        return 68;

    missingConfig:
        string missingConfigPath = Path.Combine(Path.GetDirectoryName(configPath), "first-run", "configs", "DailyTask.json");
        InvokeStatic(ensure, missingConfigPath);
        if (!File.Exists(missingConfigPath) || File.ReadAllText(missingConfigPath, Encoding.UTF8) != "{}") return 69;
        Invoke(apply, bindingType, missingConfigPath, new object[] { "干员经验低阶", "超距辉映管" });
        actual = Json.DeserializeObject(File.ReadAllText(missingConfigPath, Encoding.UTF8)) as IDictionary<string, object>;
        if (actual == null || (actual["体力本"] as string) != "干员经验" ||
            !SequenceEquals(actual["刷本序列"] as object[], new[] { "干员经验低阶", "超距辉映管" })) return 70;
        InvokeStatic(restore, missingConfigPath, null, false);
        if (File.Exists(missingConfigPath)) return 71;

        // A cancelled child must retain the fact that this config did not
        // exist before the run; recovery deletes its temporary file.
        InvokeStatic(preservePending, missingConfigPath, null, false);
        InvokeStatic(preservePending, missingConfigPath, new byte[] { 1 }, true);
        string pending = missingConfigPath + ".yeyu-restore-pending";
        if (!File.Exists(pending) || File.Exists(pending + ".absent") ||
            !Encoding.ASCII.GetString(File.ReadAllBytes(pending)).StartsWith("YEYU-OPENKURO-RESTORE-V1\nABSENT\n")) return 72;
        File.WriteAllText(missingConfigPath, "{}", new UTF8Encoding(false));
        object pendingBinding = Activator.CreateInstance(bindingType, true);
        bindingType.GetField("DailyConfig", BindingFlags.Public | BindingFlags.Instance).SetValue(pendingBinding, missingConfigPath);
        bindingType.GetField("Executable", BindingFlags.Public | BindingFlags.Instance).SetValue(pendingBinding, runnerPath);
        InvokeStatic(restorePending, pendingBinding);
        if (File.Exists(missingConfigPath) || File.Exists(pending) || File.Exists(pending + ".absent")) return 73;

        // Older candidates wrote raw original bytes; recovery remains safe.
        byte[] legacy = new byte[] { 1, 2, 3, 4 };
        File.WriteAllText(missingConfigPath, "{}", new UTF8Encoding(false));
        File.WriteAllBytes(pending, legacy);
        InvokeStatic(restorePending, pendingBinding);
        if (!File.ReadAllBytes(missingConfigPath).SequenceEqual(legacy) || File.Exists(pending)) return 74;

        // A real share lock makes the normal restore write fail. The runner
        // must retain a durable pending record before it rethrows the error.
        string lockedConfig = Path.Combine(Path.GetDirectoryName(configPath), "locked-DailyTask.json");
        byte[] lockedOriginal = new byte[] { 9, 8, 7, 6 };
        bool writeFailed = false;
        using (var held = new FileStream(lockedConfig, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None))
        {
            try { InvokeStatic(restoreOrPreserve, lockedConfig, lockedOriginal, true); }
            catch (IOException) { writeFailed = true; }
            if (!writeFailed || !File.Exists(lockedConfig + ".yeyu-restore-pending")) return 75;
        }
        bindingType.GetField("DailyConfig", BindingFlags.Public | BindingFlags.Instance).SetValue(pendingBinding, lockedConfig);
        InvokeStatic(restorePending, pendingBinding);
        if (!File.ReadAllBytes(lockedConfig).SequenceEqual(lockedOriginal) ||
            File.Exists(lockedConfig + ".yeyu-restore-pending")) return 76;
        Console.WriteLine("{\"suite\":\"endfield-profile\",\"passed\":true,\"missingConfigRestored\":true,\"pendingFormat\":\"single-record\",\"gameStarted\":false}");
        return 0;
    }

    private static void Invoke(MethodInfo apply, Type bindingType, string configPath, object[] rotation)
    {
        object binding = Activator.CreateInstance(bindingType, true);
        bindingType.GetField("DailyConfig", BindingFlags.Public | BindingFlags.Instance).SetValue(binding, configPath);
        bindingType.GetField("DailyTaskProfile", BindingFlags.Public | BindingFlags.Instance).SetValue(binding,
            new Dictionary<string, object> {
                { "staminaStage", "干员经验" }, { "rewardTier", "高阶" },
                { "staminaRotationStartDate", "2026-04-06" }, { "staminaRotation", rotation }, { "teamSlot", "2" }
            });
        try { apply.Invoke(null, new[] { binding }); }
        catch (TargetInvocationException error) {
            if (error.InnerException is InvalidOperationException) throw error.InnerException;
            throw;
        }
    }

    private static void InvokeStatic(MethodInfo method, params object[] arguments)
    {
        try { method.Invoke(null, arguments); }
        catch (TargetInvocationException error) {
            if (error.InnerException != null) throw error.InnerException;
            throw;
        }
    }

    private static bool SequenceEquals(object[] actual, string[] expected)
    {
        if (actual == null || actual.Length != expected.Length) return false;
        for (int index = 0; index < expected.Length; index++) if ((actual[index] as string) != expected[index]) return false;
        return true;
    }
}
