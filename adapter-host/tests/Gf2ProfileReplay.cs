using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Web.Script.Serialization;

internal static class Gf2ProfileReplay
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

    public static int Main(string[] args)
    {
        if (args == null || args.Length != 2) return 64;
        string runnerPath = Path.GetFullPath(args[0]);
        string configPath = Path.GetFullPath(args[1]);
        Assembly runner = Assembly.LoadFrom(runnerPath);
        Type program = runner.GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        MethodInfo status = program.GetMethod("ResolveRunStatus", BindingFlags.NonPublic | BindingFlags.Static);
        if (status == null) throw new InvalidOperationException("Missing run outcome projection");
        var outcomes = new[] {
            Tuple.Create(new object[] { false, true, false, true, false }, "failed"),
            Tuple.Create(new object[] { false, true, true, true, false }, "failed"),
            Tuple.Create(new object[] { false, false, true, true, false }, "completed"),
            Tuple.Create(new object[] { false, false, false, true, false }, "review_required"),
            Tuple.Create(new object[] { false, false, true, false, false }, "failed"),
            Tuple.Create(new object[] { true, true, false, true, false }, "cancelled"),
            Tuple.Create(new object[] { false, true, false, false, true }, "human_required"),
            Tuple.Create(new object[] { true, true, false, false, true }, "cancelled")
        };
        foreach (var outcome in outcomes)
            if ((string)status.Invoke(null, outcome.Item1) != outcome.Item2)
                throw new InvalidOperationException("Official failure/transport/cancellation outcome mismatch");
        Type bindingType = program.GetNestedType("Binding", BindingFlags.NonPublic);
        MethodInfo apply = program.GetMethod("ApplyGf2DailyTaskProfile", BindingFlags.NonPublic | BindingFlags.Static);
        MethodInfo applySelected = program.GetMethod("ApplySelectedOfficialDailyConfig", BindingFlags.NonPublic | BindingFlags.Static);
        MethodInfo mainOnlySelected = program.GetMethod("Gf2MainOnlyOperationsSelected", BindingFlags.NonPublic | BindingFlags.Static);
        MethodInfo mainBinding = program.GetMethod("IsGf2MainSourceBinding", BindingFlags.NonPublic | BindingFlags.Static);
        MethodInfo restore = program.GetMethod("RestoreDailyConfigSnapshot", BindingFlags.NonPublic | BindingFlags.Static);
        if (bindingType == null || apply == null || applySelected == null || mainOnlySelected == null || mainBinding == null || restore == null) return 65;

        var original = new Dictionary<string, object> {
            { "邮件", true }, { "情报补给", true }, { "情报和战前补给", false }, { "闪耀星愿", false },
            { "购买免费礼包", true }, { "商店心愿单购买", true }, { "活动层", true }, { "竞技场", true }, { "已确认启用游戏内全局自动功能", true },
            { "体力本", "军备解析" }, { "刷钱本", true }, { "自主循环", true },
            { "尘烟", true }, { "当前物资关卡名称", "旧关卡" }, { "喝水", "旧喝水" }, { "吃饭", "旧吃饭" },
            { "拂晓之光补给包奖励", "数据链路" }
        };
        File.WriteAllText(configPath, Json.Serialize(original), new UTF8Encoding(false));
        byte[] originalBytes = File.ReadAllBytes(configPath);
        Invoke(apply, MakeBinding(bindingType, configPath, new Dictionary<string, object> {
            { "autoBattleConfirmed", false }, { "staminaStage", "定向" }, { "extraCreditBattle", false },
            { "autonomousLoop", false }, { "squadDust", false }, { "eventStageName", "当前活动物资" },
            { "drinkDurations", "1-2-3" }, { "mealDuration", "1.5" }
        }));
        IDictionary<string, object> actual = Read(configPath);
        if (actual == null || !IsFalse(actual, "已确认启用游戏内全局自动功能") ||
            (actual["体力本"] as string) != "定向" || !IsFalse(actual, "刷钱本") ||
            !IsFalse(actual, "自主循环") || !IsFalse(actual, "尘烟") ||
            (actual["当前物资关卡名称"] as string) != "当前活动物资" ||
            (actual["喝水"] as string) != "1-2-3" || (actual["吃饭"] as string) != "1.5" ||
            !(actual["邮件"] is bool) || !(bool)actual["邮件"] || !IsFalse(actual, "情报和战前补给") ||
            !IsFalse(actual, "闪耀星愿") || !IsTrue(actual, "购买免费礼包")) return 66;

        // Sparse/null values leave all unrelated official values untouched;
        // false is still an explicit, authoritative override.
        File.WriteAllText(configPath, Json.Serialize(original), new UTF8Encoding(false));
        Invoke(apply, MakeBinding(bindingType, configPath, new Dictionary<string, object> {
            { "extraCreditBattle", false }, { "eventStageName", null }, { "mealDuration", null }
        }));
        actual = Read(configPath);
        if (actual == null || !IsFalse(actual, "刷钱本") || !IsTrue(actual, "已确认启用游戏内全局自动功能") ||
            (actual["体力本"] as string) != "军备解析" || !IsTrue(actual, "自主循环") ||
            !IsTrue(actual, "尘烟") || (actual["当前物资关卡名称"] as string) != "旧关卡" ||
            (actual["吃饭"] as string) != "旧吃饭") return 67;

        // The four catalog operations project only to their published GF2
        // switches. Unselected sensitive/non-catalog switches stay in the
        // official document instead of being adapter-authored defaults.
        File.WriteAllText(configPath, Json.Serialize(original), new UTF8Encoding(false));
        object selectedBinding = MakeBinding(bindingType, configPath, null);
        bindingType.GetField("GameId", BindingFlags.Public | BindingFlags.Instance).SetValue(selectedBinding, "GF2");
        Invoke(applySelected, selectedBinding, new[] {
            "activity-supply", "star-wish", "event-battle", "exploration-claim"
        });
        actual = Read(configPath);
        if (actual == null || !IsFalse(actual, "情报补给") || !IsTrue(actual, "情报和战前补给") || !IsTrue(actual, "闪耀星愿") ||
            !IsTrue(actual, "活动自律") || !IsTrue(actual, "探索领取") || !IsFalse(actual, "邮件") ||
            !IsFalse(actual, "公共区/调度室") || !IsFalse(actual, "自动刷体力") ||
            !IsFalse(actual, "班组") || !IsFalse(actual, "领任务") || !IsFalse(actual, "大月卡") ||
            !IsFalse(actual, "购买免费礼包") || !IsFalse(actual, "商店心愿单购买") ||
            !IsFalse(actual, "活动层") || !IsFalse(actual, "竞技场")) return 68;

        object legacyBinding = MakeBinding(bindingType, configPath, null);
        object mainSourceBinding = MakeBinding(bindingType, configPath, null);
        bindingType.GetField("AppName", BindingFlags.Public | BindingFlags.Instance).SetValue(mainSourceBinding, "ok-gf2-official-source-candidate");
        if (!(bool)Invoke(mainOnlySelected, new object[] { new[] { "activity-supply" } }) ||
            (bool)Invoke(mainOnlySelected, new object[] { new[] { "mail", "spend-stamina" } }) ||
            (bool)Invoke(mainBinding, legacyBinding) || !(bool)Invoke(mainBinding, mainSourceBinding)) return 69;

        if (!Rejects(apply, MakeBinding(bindingType, configPath, new Dictionary<string, object> { { "unknown", true } }))) return 70;
        if (!Rejects(apply, MakeBinding(bindingType, configPath, new Dictionary<string, object> { { "staminaStage", "未定义" } }))) return 71;
        if (!Rejects(apply, MakeBinding(bindingType, configPath, new Dictionary<string, object> { { "eventStageName", new string('x', 129) } }))) return 72;

        // No choice is inferred. Null keeps the saved upstream value, an
        // explicit blank keeps manual selection, and only the pinned official
        // version with this declared parameter may receive a saved choice.
        foreach (string choice in new[] { "", "数据链路", "坍塌晶条", "增域存量条T2", "大容量内存条", "转录导体·序三", "萨狄斯金" }) {
            File.WriteAllBytes(configPath, originalBytes);
            object choiceBinding = MakeBinding(bindingType, configPath, new Dictionary<string, object> { { "supplyRewardChoice", choice } });
            bindingType.GetField("Gf2SupplyRewardChoiceSupported").SetValue(choiceBinding, true);
            Invoke(apply, choiceBinding);
            if ((Read(configPath)["拂晓之光补给包奖励"] as string) != choice) return 74;
            Invoke(restore, configPath, originalBytes, true);
            if (!File.ReadAllBytes(configPath).SequenceEqual(originalBytes)) return 75;
        }
        Invoke(apply, MakeBinding(bindingType, configPath, new Dictionary<string, object> { { "supplyRewardChoice", null } }));
        if ((Read(configPath)["拂晓之光补给包奖励"] as string) != "数据链路") return 76;
        byte[] beforeRejected = File.ReadAllBytes(configPath);
        foreach (object invalid in new object[] { "unknown", true, 1, " 数据链路" }) {
            object choiceBinding = MakeBinding(bindingType, configPath, new Dictionary<string, object> { { "supplyRewardChoice", invalid } });
            bindingType.GetField("Gf2SupplyRewardChoiceSupported").SetValue(choiceBinding, true);
            if (!Rejects(apply, choiceBinding) || !File.ReadAllBytes(configPath).SequenceEqual(beforeRejected)) return 77;
        }
        if (!Rejects(apply, MakeBinding(bindingType, configPath, new Dictionary<string, object> { { "supplyRewardChoice", "数据链路" } })) ||
            !File.ReadAllBytes(configPath).SequenceEqual(beforeRejected)) return 78;

        File.WriteAllText(configPath, "{}", new UTF8Encoding(false));
        Invoke(restore, configPath, originalBytes, true);
        if (!File.ReadAllBytes(configPath).SequenceEqual(originalBytes)) return 73;
        Console.WriteLine("{\"suite\":\"gf2-profile\",\"passed\":true,\"falseOverride\":true,\"nullPreserved\":true,\"selectedOperationsProjected\":true,\"mainSourceGuard\":true,\"configRestored\":true,\"gameStarted\":false}");
        return 0;
    }

    private static object MakeBinding(Type bindingType, string configPath, IDictionary<string, object> profile)
    {
        object binding = Activator.CreateInstance(bindingType, true);
        bindingType.GetField("DailyConfig", BindingFlags.Public | BindingFlags.Instance).SetValue(binding, configPath);
        bindingType.GetField("DailyTaskProfile", BindingFlags.Public | BindingFlags.Instance).SetValue(binding, profile);
        return binding;
    }
    private static IDictionary<string, object> Read(string path)
    {
        return Json.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as IDictionary<string, object>;
    }
    private static bool IsFalse(IDictionary<string, object> value, string key) { return value.ContainsKey(key) && value[key] is bool && !(bool)value[key]; }
    private static bool IsTrue(IDictionary<string, object> value, string key) { return value.ContainsKey(key) && value[key] is bool && (bool)value[key]; }
    private static bool Rejects(MethodInfo apply, object binding)
    {
        try { Invoke(apply, binding); return false; }
        catch (InvalidOperationException) { return true; }
    }
    private static object Invoke(MethodInfo method, params object[] values)
    {
        try { return method.Invoke(null, values); }
        catch (TargetInvocationException error) { throw error.InnerException ?? error; }
    }
}
