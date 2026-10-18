using System;
using System.Collections.Generic;
using System.Reflection;
using System.Linq;
using System.IO;
using System.Diagnostics;

internal static class WwDailySelectionReplay
{
    private static readonly string[] Whole = {
        "attach-world", "inspect-daily-progress", "farm-nightmare-daily-echo", "spend-waveplates",
        "claim-daily-reward", "claim-mail", "claim-battle-pass"
    };
    public static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--fake-upstream") return FakeUpstream(args);
        var program = Assembly.LoadFrom(args[0]).GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        var selection = program.GetMethod("WwSelectionIsWholeOfficialDaily", BindingFlags.NonPublic | BindingFlags.Static);
        Check(selection, Whole, Whole, true);
        Check(selection, Whole, null, true);
        Check(selection, Whole, new string[0], true);
        Check(selection, new[] { "attach-world" }, Whole, false);
        Check(selection, new[] { "claim-mail" }, Whole, false);
        Check(selection, Whole, new[] { "attach-world" }, false);
        Check(selection, new string[0], Whole, false);
        Check(selection, new[] { "gacha" }, Whole, false);
        Check(selection, Whole.Where(x => x != "inspect-daily-progress").ToArray(), Whole, true);
        Check(selection, Whole.Where(x => x != "attach-world").ToArray(), Whole, true);
        Check(selection, Whole.Where(x => x != "attach-world" && x != "inspect-daily-progress").ToArray(), Whole, true);
        foreach (string operation in Whole.Where(x => x != "attach-world" && x != "inspect-daily-progress"))
            Check(selection, Whole.Where(x => x != operation).ToArray(), Whole, false);
        var inspect = program.GetMethod("WwShouldUseInspection", BindingFlags.NonPublic | BindingFlags.Static);
        foreach (bool supported in new[] { false, true }) {
            foreach (string[] actual in new[] { Whole, new[] { "inspect-daily-progress", "claim-daily-reward" }, new[] { "inspect-daily-progress" },
                new[] { "claim-daily-reward" }, new[] { "inspect-daily-progress", "gacha" } }) {
                bool result = (bool)inspect.Invoke(null, new object[] { actual, new HashSet<string>(Whole), supported });
                bool expected = supported && actual.SequenceEqual(new[] { "inspect-daily-progress" });
                if (result != expected) throw new Exception("Inspection selection would replay claims or authorize an unpinned tool");
            }
        }
        Type bindingType = program.GetNestedType("Binding",BindingFlags.NonPublic);
        object binding = Activator.CreateInstance(bindingType,true);
        foreach (string field in new[] { "WwInspectionSupported","WwRewardInspectionSupported","WwBattlePassInspectionSupported" })
            bindingType.GetField(field).SetValue(binding,true);
        bindingType.GetField("GameId").SetValue(binding,"WW");
        MethodInfo groups = program.GetMethod("WwInspectionGroups",BindingFlags.NonPublic | BindingFlags.Static);
        string[] requested = { "claim-battle-pass","attach-world","claim-daily-reward","inspect-daily-progress" };
        var partition = (List<string[]>)groups.Invoke(null,new object[] { binding,requested });
        string[] order = { "inspect-daily-progress","claim-daily-reward","claim-battle-pass" };
        if (partition.Count != 3 || !partition.SelectMany(group=>group).Where(value=>value!="attach-world").SequenceEqual(order) ||
            partition.Any(group=>group.Length!=2 || group[0]!="attach-world"))
            throw new Exception("Requested inspections must all run once in a fixed bounded order");
        foreach (string[] invalid in new[] { Whole,new[] { "inspect-daily-progress","spend-waveplates" },
            new[] { "claim-daily-reward","claim-daily-reward" },new[] { "claim-battle-pass","gacha" } })
            if (((List<string[]>)groups.Invoke(null,new object[] { binding,invalid })).Count != 0)
                throw new Exception("Readonly scope must never discard an ordinary or unknown operation");
        bindingType.GetField("WwBattlePassInspectionSupported").SetValue(binding,false);
        if (((List<string[]>)groups.Invoke(null,new object[] { binding,requested })).Count != 0)
            throw new Exception("An unpinned requested inspection cannot be silently dropped");
        try {
            SharedObservationReplay(program, bindingType, args[0]);
            PostDailyReplay(program, bindingType, args[0]);
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
        Console.WriteLine("{\"status\":\"passed\",\"cases\":33,\"gameStarted\":false}");
        return 0;
    }
    private static void SharedObservationReplay(Type program, Type bindingType, string runner) {
        string root = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(runner)), "ww-shared-replay-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        string previousStaging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
        try {
            Environment.SetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR", root);
            string config = Path.Combine(root, "daily.json"), ledger = Path.Combine(root, "processes.log");
            string observations = Path.Combine(root, "observations.jsonl");
            File.WriteAllText(config, "{\"retained\":true}");
            File.WriteAllText(observations, "{\"operation\":\"upstream-daily-task\",\"state\":\"completed\",\"account\":\"test-saved-account\",\"detail\":\"official return\"}\n");
            object binding = Activator.CreateInstance(bindingType, true);
            foreach (string flag in new[] { "WwInspectionSupported", "WwRewardInspectionSupported", "WwBattlePassInspectionSupported" })
                bindingType.GetField(flag).SetValue(binding, true);
            bindingType.GetField("AccountSnapshot").SetValue(binding,
                new Dictionary<string, object> { { "saved_account_label", "test-saved-account" } });
            var values = new Dictionary<string, string> {
                { "GameId", "WW" }, { "ToolRoot", root }, { "WorkingDirectory", root }, { "DailyConfig", config },
                { "Executable", Assembly.GetExecutingAssembly().Location },
                { "Arguments", "--fake-upstream diagnostic \"" + ledger + "\" " + Process.GetCurrentProcess().Id }
            };
            foreach (var value in values) bindingType.GetField(value.Key).SetValue(binding, value.Value);
            int observed = 0;
            object[] arguments = { binding, Whole, new Action<string,string,string>((operation,state,detail) => observed++),
                new Func<bool>(() => false), true, observations, null, false };
            int exit = (int)program.GetMethod("RunDaily", BindingFlags.NonPublic | BindingFlags.Static).Invoke(null, arguments);
            if (exit != 0 || (bool)arguments[7] || observed != 1 || File.Exists(ledger))
                throw new Exception("Shared WW observations must not start post-daily inspection processes without a managed game launch.");
            if (File.ReadAllText(config) != "{\"retained\":true}")
                throw new Exception("Shared WW replay changed saved configuration.");
        } finally {
            Environment.SetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR", previousStaging);
            Directory.Delete(root, true);
        }
    }
    private static int FakeUpstream(string[] args) {
        string scope = Environment.GetEnvironmentVariable("YEYU_GAMER_SELECTED_OPERATIONS") ?? "";
        string stage = Environment.GetEnvironmentVariable("YEYU_GAMER_STAGE_FILE");
        string scenario = args[1];
        string ledger = args[2];
        File.AppendAllText(ledger, Process.GetCurrentProcess().Id + ":" + args[3] + ":" + scope + Environment.NewLine);
        string failedOperation = scenario == "diagnostic" ? "ww-login-window-click" :
            scenario == "activation" ? "login-window-activation" :
            scenario == "unknown" ? "unknown-source" : "claim-daily-reward";
        string state = scenario == "human" ? "human_required" : scenario == "uncertain" ? "uncertain" : "failed";
        string records = "";
        if (scope.Contains("farm-nightmare-daily-echo") && scenario != "no-success")
            records += "{\"operation\":\"" + failedOperation + "\",\"state\":\"" + state + "\",\"detail\":\"replay\"}\n";
        records += "{\"operation\":\"" + (scenario == "multi-none" ? "upstream-multi-account-task" : "upstream-daily-task") + "\",\"state\":\"" +
            (scenario == "no-success" ? "started" : "completed") + "\",\"detail\":\"replay\"}\n";
        if (scenario == "multi-none")
            records += "{\"operation\":\"upstream-multi-account-task\",\"state\":\"completed\",\"detail\":\"upstream:MultiAccountDailyTask.run:returned:None\"}\n";
        File.WriteAllText(stage, records);
        return 0;
    }
    private static void PostDailyReplay(Type program, Type bindingType, string runner) {
        string root = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(runner)), "ww-post-daily-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        string previousStaging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
        try {
            Environment.SetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR", root);
            foreach (string scenario in new[] { "diagnostic", "activation", "task", "unknown", "human", "uncertain", "no-success", "multi-none" }) {
                object binding = Activator.CreateInstance(bindingType, true);
                string config = Path.Combine(root, scenario + ".json"), ledger = Path.Combine(root, scenario + ".processes.log");
                File.WriteAllText(config, "{\"retained\":true}");
                foreach (string flag in new[] { "WwInspectionSupported", "WwRewardInspectionSupported", "WwBattlePassInspectionSupported" })
                    bindingType.GetField(flag).SetValue(binding, true);
                bindingType.GetField("AccountSnapshot").SetValue(binding,
                    new Dictionary<string, object> { { "saved_account_label", "test-saved-account" } });
                var values = new Dictionary<string, string> {
                    { "GameId", "WW" }, { "ToolRoot", root },
                    { "WorkingDirectory", root }, { "DailyConfig", config },
                    { "Executable", Assembly.GetExecutingAssembly().Location },
                    { "Arguments", "--fake-upstream " + scenario + " \"" + ledger + "\" " + Process.GetCurrentProcess().Id }
                };
                foreach (var value in values) bindingType.GetField(value.Key).SetValue(binding, value.Value);
                object[] arguments = { binding, Whole, new Action<string,string,string>((operation,state,detail) => {}),
                    new Func<bool>(() => false), false, "", null, false };
                int exit = (int)program.GetMethod("RunDaily", BindingFlags.NonPublic | BindingFlags.Static).Invoke(null, arguments);
                int expected = 1;
                if (exit != (scenario == "human" ? 30 : 0) || (bool)arguments[7] || File.ReadAllLines(ledger).Length != expected)
                    throw new Exception("WW post-daily dispatch violated official failure/diagnostic boundary: " + scenario);
                if (File.ReadAllText(config) != "{\"retained\":true}")
                    throw new Exception("WW post-daily replay changed saved configuration: " + scenario);
                foreach (string flag in new[] { "WwInspectionOnly", "WwRewardInspectionOnly", "WwBattlePassInspectionOnly" })
                    if ((bool)bindingType.GetField(flag).GetValue(binding)) throw new Exception("Inspection flag leaked: " + scenario);
            }
        } finally {
            Environment.SetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR", previousStaging);
            Directory.Delete(root, true);
        }
    }
    private static void Check(MethodInfo method, string[] actual, string[] frozen, bool expected)
    {
        var selected = frozen == null ? null : new HashSet<string>(frozen, StringComparer.Ordinal);
        bool result = (bool)method.Invoke(null, new object[] { actual, selected });
        if (result != expected) throw new Exception("Partial recovery incorrectly authorizes a whole official daily.");
    }
}
