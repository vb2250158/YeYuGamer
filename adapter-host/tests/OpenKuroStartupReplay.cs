using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Linq;
using System.Web.Script.Serialization;

// Execute the compiled production launch loop using an inert child process.
internal static class OpenKuroStartupReplay
{
    static int Main(string[] args)
    {
        if (args.Length == 1) {
            if (args[0] == "--ww-inspection" || args[0] == "--ww-ordinary" || args[0] == "--ww-ordinary-false") {
                var json = new JavaScriptSerializer();
                var parsedDailyConfig = json.DeserializeObject(File.ReadAllText(Path.Combine(Environment.CurrentDirectory, "DailyTask.json"))) as Dictionary<string, object>;
                string[] flags = { "Daily Progress Inspection Only", "Daily Reward Status Inspection Only", "Battle Pass Status Inspection Only" };
                string[] operations = { "inspect-daily-progress", "claim-daily-reward", "claim-battle-pass" };
                int[] enabled = Enumerable.Range(0, flags.Length).Where(index => parsedDailyConfig.ContainsKey(flags[index]) && parsedDailyConfig[flags[index]] is bool && (bool)parsedDailyConfig[flags[index]]).ToArray();
                if (enabled.Length == 0 && args[0].StartsWith("--ww-ordinary",StringComparison.Ordinal)) {
                    File.AppendAllText(Path.Combine(Environment.CurrentDirectory,"inspection-entries.txt"),"ordinary\n");
                    File.AppendAllText(Environment.GetEnvironmentVariable("YEYU_GAMER_STAGE_FILE"),
                        "{\"operation\":\"upstream-daily-task\",\"state\":\"" + (args[0] == "--ww-ordinary" ? "completed" : "observed_false") + "\",\"detail\":\"fixture official root returned\"}\n",new UTF8Encoding(false));
                    return 0;
                }
                if (enabled.Length != 1 || (string)parsedDailyConfig["user-field"] != "retained-user-value") return 98;
                string operation = operations[enabled[0]];
                var selected = json.DeserializeObject(Environment.GetEnvironmentVariable("YEYU_GAMER_SELECTED_OPERATIONS")) as object[];
                if (!selected.Cast<string>().SequenceEqual(new[] { "attach-world", operation })) return 97;
                File.AppendAllText(Path.Combine(Environment.CurrentDirectory, "inspection-entries.txt"), operation + "\n");
                string stage = Environment.GetEnvironmentVariable("YEYU_GAMER_STAGE_FILE");
                foreach (string item in new[] { "attach-world", operation })
                    File.AppendAllText(stage, "{\"operation\":\"" + item + "\",\"state\":\"started\",\"detail\":\"fixture official method entered\"}\n" +
                        "{\"operation\":\"" + item + "\",\"state\":\"completed\",\"detail\":\"fixture official method returned\"}\n", new UTF8Encoding(false));
                return 0;
            }
            if (args[0] == "--inspection" || args[0] == "--inspection-failed") {
                string observed = File.ReadAllText(Path.Combine(Environment.CurrentDirectory, "DailyTask.json"));
                if (Environment.GetEnvironmentVariable("YEYU_GAMER_ENDFIELD_REWARD_INSPECTION_ONLY") != "1" ||
                    observed.IndexOf("\"查看通行证\":true", StringComparison.Ordinal) < 0 ||
                    observed.IndexOf("retained-user-value", StringComparison.Ordinal) < 0) return 98;
                File.AppendAllText(Environment.GetEnvironmentVariable("YEYU_GAMER_STAGE_FILE"),
                    "{\"operation\":\"claim-daily-reward\",\"state\":\"started\",\"detail\":\"fixture inspection entered\"}\n" +
                    "{\"operation\":\"claim-daily-reward\",\"state\":\"" + (args[0] == "--inspection" ? "completed" : "failed") + "\",\"detail\":\"fixture inspection returned\"}\n",
                    new UTF8Encoding(false));
                return args[0] == "--inspection" ? 0 : 23;
            }
            if (args[0] == "--human") {
                File.AppendAllText(Environment.GetEnvironmentVariable("YEYU_GAMER_STAGE_FILE"),
                    "{\"operation\":\"battle-pass-free-track\",\"state\":\"started\",\"detail\":\"fixture method entered\"}\n" +
                    "{\"operation\":\"battle-pass-free-track\",\"state\":\"human_required\",\"detail\":\"gf2_supply_reward_choice_required: fixture reward choice\"}\n",
                    new UTF8Encoding(false));
                Thread.Sleep(30000);
                return 99;
            }
            if (args[0] == "--stage") {
                File.AppendAllText(Environment.GetEnvironmentVariable("YEYU_GAMER_STAGE_FILE"),
                    "{\"operation\":\"attach-home\",\"state\":\"started\",\"detail\":\"fixture method entered\"}\n",
                    new UTF8Encoding(false));
                Thread.Sleep(200);
            }
            return 0;
        }
        Type program = Assembly.LoadFrom(args[0]).GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        Type bindingType = program.GetNestedType("Binding", BindingFlags.NonPublic);
        MethodInfo run = program.GetMethod("RunDaily", BindingFlags.Static | BindingFlags.NonPublic);
        string root = Path.GetFullPath(args[1]);
        Directory.CreateDirectory(root);
        string config = Path.Combine(root, "DailyTask.json");
        File.WriteAllText(config, "{}", new UTF8Encoding(false));
        string previous = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
        Environment.SetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR", root);
        try {
            foreach (string mode in new[] { "--no-events", "--stage", "--human", "--inspection", "--inspection-failed" }) {
                bool emitStage = mode == "--stage", human = mode == "--human";
                bool inspection = mode.StartsWith("--inspection", StringComparison.Ordinal);
                string originalConfig = inspection ? "{\"查看通行证\":false,\"user-field\":\"retained-user-value\"}" : "{}";
                File.WriteAllText(config, originalConfig, new UTF8Encoding(false));
                object binding = Activator.CreateInstance(bindingType, true);
                var values = new Dictionary<string, object> {
                    { "GameId", inspection ? "Endfield" : "GF2" }, { "ToolRoot", root }, { "WorkingDirectory", root },
                    { "DailyConfig", config }, { "Executable", Assembly.GetExecutingAssembly().Location },
                    { "Arguments", mode }, { "EndfieldRewardInspectionOnly", inspection }
                };
                foreach (var value in values) bindingType.GetField(value.Key).SetValue(binding, value.Value);
                var events = new List<string>();
                Action<string, string, string> observe = (operation, state, detail) => events.Add(operation + ":" + state);
                object[] inputs = { binding, new[] { inspection ? "claim-daily-reward" : human ? "battle-pass-free-track" : "attach-home" }, observe, new Func<bool>(() => false), false, "", null, false };
                int exit = (int)run.Invoke(null, inputs);
                if (exit != (human ? 30 : mode == "--inspection-failed" ? 23 : 0) || (bool)inputs[7] || File.ReadAllText(config) != originalConfig)
                    throw new InvalidOperationException("Production launch/restore failed");
                string expected = "official-tool-start:tool_started" + (inspection ? ",claim-daily-reward:started,claim-daily-reward:" + (mode == "--inspection" ? "completed" : "failed") : human ? ",battle-pass-free-track:started,battle-pass-free-track:human_required" : emitStage ? ",attach-home:started" : "");
                if (String.Join(",", events) != expected)
                    throw new InvalidOperationException("Tool startup invented or lost an official step: " + String.Join(",", events));
            }
            foreach (bool cancelAfterFirst in new[] { false, true }) {
                string trace = Path.Combine(root,"inspection-entries.txt");
                if (File.Exists(trace)) File.Delete(trace);
                string originalConfig = "{\"user-field\":\"retained-user-value\",\"Daily Progress Inspection Only\":false}";
                File.WriteAllText(config, originalConfig, new UTF8Encoding(false));
                object binding = Activator.CreateInstance(bindingType,true);
                foreach (var value in new Dictionary<string, object> {
                    { "GameId","WW" }, { "ToolRoot",root }, { "WorkingDirectory",root },
                    { "DailyConfig",config }, { "Executable",Assembly.GetExecutingAssembly().Location },
                    { "Arguments","--ww-inspection" }, { "WwInspectionSupported",true },
                    { "WwRewardInspectionSupported",true }, { "WwBattlePassInspectionSupported",true }
                }) bindingType.GetField(value.Key).SetValue(binding,value.Value);
                var events = new List<string>();
                bool cancel = false;
                Action<string,string,string> observe = (operation,state,detail) => {
                    events.Add(operation+":"+state);
                    if (cancelAfterFirst && operation == "inspect-daily-progress" && state == "completed") cancel = true;
                };
                object[] inputs = { binding,new[] { "attach-world","inspect-daily-progress","claim-daily-reward","claim-battle-pass" },observe,
                    new Func<bool>(()=>cancel),false,"",null,false };
                int exit = (int)run.Invoke(null,inputs);
                string[] entries = File.ReadAllLines(trace);
                string[] expected = cancelAfterFirst ? new[] { "inspect-daily-progress" } : new[] { "inspect-daily-progress","claim-daily-reward","claim-battle-pass" };
                if (exit != 0 || (bool)inputs[7] != cancelAfterFirst || !entries.SequenceEqual(expected) || File.ReadAllText(config) != originalConfig)
                    throw new InvalidOperationException("WW inspection scope, bounded cancellation, or exact config restoration failed");
                if (events.Count(item=>item=="attach-world:started") != 1 || events.Count(item=>item=="attach-world:completed") != 1)
                    throw new InvalidOperationException("WW repeated invocation duplicated session-entry evidence");
                foreach (string operation in expected)
                    if (events.Count(item=>item==operation+":started") != 1 || events.Count(item=>item==operation+":completed") != 1)
                        throw new InvalidOperationException("WW inspection operation was missing or duplicated");
            }
            foreach (bool officialSuccess in new[] { false,true }) {
                string trace = Path.Combine(root,"inspection-entries.txt");
                File.Delete(trace);
                string originalConfig = "{\"user-field\":\"retained-user-value\"}";
                File.WriteAllText(config,originalConfig,new UTF8Encoding(false));
                object binding=Activator.CreateInstance(bindingType,true);
                foreach (var value in new Dictionary<string,object> {
                    { "GameId","WW" },{ "ToolRoot",root },{ "WorkingDirectory",root },
                    { "DailyConfig",config },{ "Executable",Assembly.GetExecutingAssembly().Location },
                    { "Arguments",officialSuccess ? "--ww-ordinary" : "--ww-ordinary-false" },
                    { "AccountSnapshot",new Dictionary<string,object> { { "saved_account_label","fixture-account" } } },
                    { "WwInspectionSupported",true },{ "WwRewardInspectionSupported",true },{ "WwBattlePassInspectionSupported",true }
                })bindingType.GetField(value.Key).SetValue(binding,value.Value);
                object[] inputs={binding,new[] { "attach-world","inspect-daily-progress","claim-daily-reward","claim-battle-pass","spend-waveplates","claim-mail","farm-nightmare-daily-echo" },
                    new Action<string,string,string>((operation,state,detail)=>{}),new Func<bool>(()=>false),false,"",null,false};
                int exit=(int)run.Invoke(null,inputs);
                string[] expected=new[] { "ordinary" };
                if(exit!=0 || (bool)inputs[7] || !File.ReadAllLines(trace).SequenceEqual(expected) || File.ReadAllText(config)!=originalConfig)
                    throw new InvalidOperationException("Ordinary daily must start once without implicit read-only verification");
            }
        } finally {
            Environment.SetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR", previous);
        }
        Console.WriteLine("{\"suite\":\"openkuro-startup\",\"passed\":true,\"gameStarted\":false}");
        return 0;
    }
}
