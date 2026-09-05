using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;

namespace YeYuGamer.CznAdapter
{
    internal static class Program
    {
#if BD2
        private const string GameId = "BD2";
#else
        private const string GameId = "CZN";
#endif
        private const string PackageId = "legacy-night-rain-gamer";
        private const string ProtocolVersion = "1.1";
        private const int MaaSucceeded = 3000;
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        private sealed class OperationSpec
        {
            public readonly string Entry;
            public readonly string[] CompletedNodes;
            public readonly string[] ReviewNodes;
            public readonly bool SkipWhenOnlyHome;

            public OperationSpec(string entry, string[] completedNodes, string[] reviewNodes, bool skipWhenOnlyHome)
            {
                Entry = entry;
                CompletedNodes = completedNodes;
                ReviewNodes = reviewNodes;
                SkipWhenOnlyHome = skipWhenOnlyHome;
            }
        }

        private static readonly IDictionary<string, OperationSpec> Operations =
            new Dictionary<string, OperationSpec>(StringComparer.Ordinal)
            {
#if BD2
                { "attach-home", new OperationSpec("Global_ToHomePage", new[] { "Global_ToHomePage_Enter", "Rec_HomePage_GA_Ocr", "Rec_HomePage_GA_Clr" }, new string[0], false) },
                { "daily-claim", new OperationSpec("RewardsDaily_Start|Pass_HomePage", new[] { "RewardsDaily_GetDailyReward", "RewardsDaily_Get", "Pass_GetAll", "Pass_Back" }, new string[0], false) },
                { "stamina-sweep", new OperationSpec("QuickHunt_Start", new[] { "QuickHunt_FastBattleReward", "QuickHunt_NoRiceAP_Skip" }, new string[0], false) }
#else
                {
                    "login-bonus",
                    new OperationSpec(
                        "进游戏-任务开始层",
                        new[] { "进游戏-点击领取签到奖励" },
                        new string[0],
                        true)
                },
                {
                    "achievement-schedule",
                    new OperationSpec(
                        "领奖-日常活跃-任务开始层",
                        new[] { "领奖-日常活跃-奖励领取完毕" },
                        new[] { "领奖-日常活跃-不可领取奖励" },
                        false)
                },
                {
                    "arkhianon-supply",
                    new OperationSpec(
                        "领奖-补给-任务开始层",
                        new[] { "领奖-补给-奖励获取界面-奖励领完了" },
                        new string[0],
                        false)
                },
                {
                    "simulation-stamina",
                    new OperationSpec(
                        "清体力-任务开始层",
                        new[] { "清体力-当前体力不足", "清体力-体力小于20已耗尽", "清体力-当前体力不足以进行挑战" },
                        new string[0],
                        false)
                }
#endif
            };

        private sealed class CznProfile
        {
            public string Category;
            public string Target;
            public int BattleEfficiency;
            public bool UntilExhausted;

            public IDictionary<string, object> ToDocument()
            {
                return new Dictionary<string, object>
                {
                    { "staminaCategory", Category },
                    { "staminaTarget", Target },
                    { "battleEfficiency", BattleEfficiency },
                    { "untilExhausted", UntilExhausted }
                };
            }
        }

        private sealed class ToolBinding
        {
            public string Root;
            public string GuiEntry;
            public string NativeDirectory;
            public string ResourceDirectory;
            public string AgentDirectory;
            public string AdbPath;
            public string AdbSerial;
            public CznProfile Profile;
        }

        private sealed class MaaSession : IDisposable
        {
            private readonly object sync = new object();
            private readonly HashSet<string> observedNodes = new HashSet<string>(StringComparer.Ordinal);
            private Native.EventCallback callback;
            private IntPtr controller;
            private IntPtr resource;
            private IntPtr tasker;
            private long sinkId;

            public MaaSession(ToolBinding binding)
            {
                if (!Native.SetDllDirectory(binding.NativeDirectory))
                    throw new InvalidOperationException("maa_native_directory_rejected");
                controller = Native.CreateAdbController(
                    binding.AdbPath,
                    binding.AdbSerial,
                    UInt64.MaxValue & ~(1UL << 3) & ~(1UL << 4) & ~(1UL << 5),
                    UInt64.MaxValue & ~(1UL << 3),
                    "{}",
                    binding.AgentDirectory);
                if (controller == IntPtr.Zero) throw new InvalidOperationException("maa_controller_create_failed");
                long connectionId = Native.MaaControllerPostConnection(controller);

                resource = Native.MaaResourceCreate();
                if (resource == IntPtr.Zero) throw new InvalidOperationException("maa_resource_create_failed");
                long resourceId = Native.ResourcePostBundle(resource, binding.ResourceDirectory);
                if (connectionId == 0 || resourceId == 0 ||
                    Native.MaaControllerWait(controller, connectionId) != MaaSucceeded ||
                    Native.MaaResourceWait(resource, resourceId) != MaaSucceeded)
                    throw new InvalidOperationException("maa_controller_or_resource_init_failed");

                tasker = Native.MaaTaskerCreate();
                if (tasker == IntPtr.Zero || !Native.MaaTaskerBindResource(tasker, resource) ||
                    !Native.MaaTaskerBindController(tasker, controller) || !Native.MaaTaskerInited(tasker))
                    throw new InvalidOperationException("maa_tasker_init_failed");
                callback = OnEvent;
                sinkId = Native.MaaTaskerAddSink(tasker, callback, IntPtr.Zero);
                if (sinkId == 0) throw new InvalidOperationException("maa_tasker_sink_failed");
            }

            public Tuple<int, string[]> Run(string entry, string pipelineOverride)
            {
                lock (sync) observedNodes.Clear();
                long taskId = Native.TaskerPostTask(tasker, entry, pipelineOverride);
                if (taskId == 0) return Tuple.Create(0, new string[0]);
                int status = Native.MaaTaskerWait(tasker, taskId);
                string[] nodes;
                lock (sync) nodes = observedNodes.OrderBy(value => value, StringComparer.Ordinal).ToArray();
                return Tuple.Create(status, nodes);
            }

            private void OnEvent(IntPtr handle, IntPtr messagePointer, IntPtr detailPointer, IntPtr transArg)
            {
                string message = Native.Utf8String(messagePointer);
                if (message != "Node.Recognition.Succeeded" && message != "Node.PipelineNode.Succeeded" &&
                    message != "Node.Action.Succeeded") return;
                try
                {
                    IDictionary<string, object> detail = Json.DeserializeObject(Native.Utf8String(detailPointer)) as IDictionary<string, object>;
                    string name = detail == null ? null : detail["name"] as string;
                    if (!String.IsNullOrWhiteSpace(name)) lock (sync) observedNodes.Add(name);
                }
                catch { }
            }

            public void Dispose()
            {
                if (tasker != IntPtr.Zero && sinkId != 0) Native.MaaTaskerRemoveSink(tasker, sinkId);
                if (tasker != IntPtr.Zero) Native.MaaTaskerDestroy(tasker);
                if (resource != IntPtr.Zero) Native.MaaResourceDestroy(resource);
                if (controller != IntPtr.Zero) Native.MaaControllerDestroy(controller);
                tasker = IntPtr.Zero;
                resource = IntPtr.Zero;
                controller = IntPtr.Zero;
                sinkId = 0;
                callback = null;
                Native.SetDllDirectory(null);
            }
        }

        public static int Main(string[] args)
        {
            if (args != null && args.Length == 1 && args[0] == "--probe-binding") return ProbeBinding();
            if (args != null && args.Length == 1 && args[0] == "--validate-profile") return ValidateProfile();

            Dictionary<string, string> options = ParseArguments(args);
            if (options == null || options["--protocol-version"] != ProtocolVersion || options["--game-id"] != GameId) return 64;
            IDictionary<string, object> request;
            try { request = Json.DeserializeObject(Console.In.ReadLine() ?? String.Empty) as IDictionary<string, object>; }
            catch { return 64; }
            if (!MatchesRequest(request, options)) return 64;

            object[] ids = request["executableTodoInstanceIds"] as object[];
            object[] todos = request["todos"] as object[];
            if (ids == null || todos == null || ids.Length == 0 || ids.Length != todos.Length) return 64;

            var selected = new List<Tuple<string, string, OperationSpec>>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            for (int index = 0; index < ids.Length; index++)
            {
                string id = ids[index] as string;
                IDictionary<string, object> todo = todos[index] as IDictionary<string, object>;
                string operation = todo == null ? null : todo["operation"] as string;
                OperationSpec spec;
                if (String.IsNullOrWhiteSpace(id) || String.IsNullOrWhiteSpace(operation) || !seen.Add(operation) ||
                    !Operations.TryGetValue(operation, out spec)) return 64;
                selected.Add(Tuple.Create(id, operation, spec));
            }

            ToolBinding binding;
            try { binding = LoadBinding(true); }
            catch (Exception error) { return TerminalSetupFailure(request, ids, error.Message); }

            string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            if (String.IsNullOrWhiteSpace(staging) || !Directory.Exists(staging))
                return TerminalSetupFailure(request, ids, "adapter_staging_missing");

            Emit(Base(request, "hello", 0, new Dictionary<string, object>
            {
                { "packageId", PackageId }, { "packageVersion", PackageVersion() },
                { "packageDigest", PackageDigest() }, { "runnerPid", Process.GetCurrentProcess().Id },
                { "acceptedTodoInstanceIds", ids }
            }));

            int sequence = 1;
            var attempts = new Dictionary<string, string>(StringComparer.Ordinal);
            var completed = new HashSet<string>(StringComparer.Ordinal);
            var resolved = new HashSet<string>(StringComparer.Ordinal);
            bool transportFailed = false;

            try
            {
                EnsureFormalGui(binding);
                using (var maa = new MaaSession(binding))
                {
                    foreach (Tuple<string, string, OperationSpec> item in selected)
                    {
                        string id = item.Item1;
                        string operation = item.Item2;
                        string attempt = Guid.NewGuid().ToString();
                        attempts[id] = attempt;
                        Emit(Base(request, "todo_attempt_started", sequence++, new Dictionary<string, object>
                        {
                            { "todoInstanceId", id }, { "todoAttemptId", attempt }, { "attemptNo", 1 }, { "operation", operation }
                        }));

                        string pipelineOverride = operation == "simulation-stamina" ? BuildStaminaOverride(binding.Profile) :
                            operation == "stamina-sweep" ? "{\"QuickHunt_AdventureRoute\":{\"enabled\":false},\"QuickHunt_CrystalCave\":{\"enabled\":false},\"QuickHunt_HuntingGrounds\":{\"enabled\":true},\"QuickHunt_RiceRecheck_HuntingGrounds_Entry\":{\"enabled\":true}}" : "{}";
                        var aggregateNodes = new List<string>();
                        int aggregateStatus = MaaSucceeded;
                        foreach (string entry in item.Item3.Entry.Split('|'))
                        {
                            Tuple<int, string[]> part = maa.Run(entry, pipelineOverride);
                            aggregateNodes.AddRange(part.Item2);
                            if (part.Item1 != MaaSucceeded) { aggregateStatus = part.Item1; break; }
                        }
                        Tuple<int, string[]> result = Tuple.Create(aggregateStatus, aggregateNodes.Distinct(StringComparer.Ordinal).ToArray());
                        string state;
                        string reasonCode;
                        string reason;
                        bool retryable = false;
                        if (result.Item1 != MaaSucceeded)
                        {
                            state = "blocked";
                            reasonCode = "maa_task_failed";
                            reason = "Maa task status=" + result.Item1;
                            retryable = true;
                            transportFailed = true;
                        }
                        else if (item.Item3.CompletedNodes.Any(result.Item2.Contains))
                        {
                            state = "completed";
                            reasonCode = "maa_terminal_node_observed";
                            reason = "Observed required terminal node";
                            completed.Add(id);
                        }
                        else if (item.Item3.ReviewNodes.Any(result.Item2.Contains))
                        {
                            state = "review_required";
                            reasonCode = "maa_route_not_complete";
                            reason = "Maa observed an explicit not-complete state";
                        }
                        else if (item.Item3.SkipWhenOnlyHome && result.Item2.Contains("已进入首页-结束任务"))
                        {
                            state = "skipped";
                            reasonCode = "already_claimed_or_not_present";
                            reason = "Reached home without a login-reward claim node";
                            resolved.Add(id);
                        }
                        else
                        {
                            state = "review_required";
                            reasonCode = "maa_terminal_evidence_missing";
                            reason = "Maa task succeeded without the required business terminal node";
                        }
                        if (state == "completed") resolved.Add(id);

                        string artifactId = StageEvidence(staging, operation, binding.Profile, result.Item1, result.Item2, id, attempt, request, ref sequence);
                        Emit(Base(request, "todo_terminal", sequence++, new Dictionary<string, object>
                        {
                            { "todoInstanceId", id }, { "todoAttemptId", attempt }, { "status", state },
                            { "reasonCode", reasonCode }, { "reason", reason }, { "retryable", retryable },
                            { "evidenceArtifactIds", new[] { artifactId } }
                        }));
                    }
                }
            }
            catch (Exception error)
            {
                transportFailed = true;
                Console.Error.WriteLine("maa_runtime_failed:" + SafeDetail(error.Message));
            }

            string runStatus = transportFailed ? "failed" : resolved.Count == ids.Length ? "completed" : "review_required";
            string[] attemptedIds = ids.Cast<string>().Where(attempts.ContainsKey).ToArray();
            string[] completedIds = ids.Cast<string>().Where(completed.Contains).ToArray();
            string[] unresolvedIds = ids.Cast<string>().Where(id => !completed.Contains(id)).ToArray();
            Emit(Base(request, "run_terminal", sequence, new Dictionary<string, object>
            {
                { "status", runStatus }, { "transportOutcome", "clean" },
                { "attemptedTodoInstanceIds", attemptedIds },
                { "completedTodoInstanceIds", completedIds },
                { "unresolvedTodoInstanceIds", unresolvedIds },
                { "terminalEventDigest", TerminalDigest(request, runStatus, attemptedIds, completedIds, unresolvedIds, 0) }, { "exitCode", 0 }
            }));
            return 0;
        }

        private static string StageEvidence(string staging, string operation, CznProfile profile, int maaStatus,
            string[] nodes, string id, string attempt, IDictionary<string, object> request, ref int sequence)
        {
            string fileName = "czn-maa-" + Guid.NewGuid().ToString("N") + ".json";
            string path = Path.Combine(staging, fileName);
            var document = new Dictionary<string, object>
            {
                { "schemaVersion", 1 }, { "operation", operation }, { "maaStatus", maaStatus },
                { "observedTerminalCandidates", nodes }, { "staminaProfile", profile == null ? null : profile.ToDocument() },
                { "capturedAt", DateTime.UtcNow.ToString("o") }
            };
            File.WriteAllText(path, Json.Serialize(document), new UTF8Encoding(false));
            string artifactId = Guid.NewGuid().ToString();
            Emit(Base(request, "artifact_staged", sequence++, new Dictionary<string, object>
            {
                { "todoInstanceId", id }, { "todoAttemptId", attempt }, { "artifactId", artifactId },
                { "kind", "tool-log-outcome" }, { "fileName", fileName }, { "mimeType", "text/plain" },
                { "sizeBytes", new FileInfo(path).Length }, { "sha256", Hash(path) }, { "capturedAt", DateTime.UtcNow.ToString("o") }
            }));
            return artifactId;
        }

        private static string BuildStaminaOverride(CznProfile profile)
        {
            string categoryNode = "清体力-" + profile.Category;
            string selectionNode = categoryNode + "-刷取选择接口";
            string template = "日常/清体力/" + profile.Category + "/" + profile.Target + ".png";
            var result = new Dictionary<string, object>
            {
                {
                    "清体力-刷取类型接口",
                    new Dictionary<string, object> { { "next", new[] { categoryNode + "-任务开始层" } } }
                },
                {
                    selectionNode,
                    new Dictionary<string, object> { { "template", new[] { template } } }
                },
                {
                    "清体力-战斗效率-单次刷取次数-重置成功",
                    new Dictionary<string, object>
                    {
                        { "next", new[] { "清体力-当前体力不足", "清体力-战斗效率-单次刷取次数已选择_" + profile.BattleEfficiency + "次", "[JumpBack]清体力-战斗效率-挑战次数+1" } }
                    }
                }
            };
            if ((profile.Category == "主战员" || profile.Category == "辅战员") &&
                (profile.Target == "奥义师" || profile.Target == "操控师"))
                result[categoryNode + "-刷取选择-找不到下面-滑动"] = new Dictionary<string, object> { { "enabled", true } };
            if (profile.Category == "潜能" && profile.Target == "正义")
                result[categoryNode + "-刷取选择-找不到下面-滑动"] = new Dictionary<string, object> { { "enabled", true } };
            return Json.Serialize(result);
        }

        private static CznProfile ParseProfile(IDictionary<string, object> value)
        {
            if (value == null || value.Count != 4 || !(value["staminaCategory"] is string) ||
                !(value["staminaTarget"] is string) || !(value["battleEfficiency"] is int) ||
                !(value["untilExhausted"] is bool)) throw new InvalidOperationException("czn_daily_profile_invalid");
            string category = value["staminaCategory"] as string;
            string target = value["staminaTarget"] as string;
            int efficiency = (int)value["battleEfficiency"];
            bool untilExhausted = (bool)value["untilExhausted"];
            var targets = new Dictionary<string, string[]>(StringComparer.Ordinal)
            {
                { "成长", new[] { "单元币", "主战员升级材料", "辅战员升级材料" } },
                { "主战员", new[] { "前锋", "守卫", "游侠", "猎人", "奥义师", "操控师" } },
                { "辅战员", new[] { "前锋", "守卫", "游侠", "猎人", "奥义师", "操控师" } },
                { "潜能", new[] { "热情", "秩序", "本能", "虚无", "正义" } }
            };
            string[] allowed;
            if (!targets.TryGetValue(category, out allowed) || !allowed.Contains(target) ||
                efficiency < 1 || efficiency > 5 || !untilExhausted)
                throw new InvalidOperationException("czn_daily_profile_invalid_or_unsafe");
            return new CznProfile { Category = category, Target = target, BattleEfficiency = efficiency, UntilExhausted = true };
        }

        private static ToolBinding LoadBinding(bool requireManagerBinding)
        {
            string packageRoot = AppDomain.CurrentDomain.BaseDirectory;
            IDictionary<string, object> package = Json.DeserializeObject(File.ReadAllText(Path.Combine(packageRoot, "tool-binding.json"), Encoding.UTF8)) as IDictionary<string, object>;
            IDictionary<string, object> tool = package == null ? null : package["tool"] as IDictionary<string, object>;
            if (package == null || tool == null || !NumberEquals(package["schemaVersion"], 1) || package["gameId"] as string != GameId)
                throw new InvalidOperationException("package_binding_invalid");
            string root = LocalDirectory(tool["root"] as string, "configured_tool_missing");
            string native = LocalDirectory(Path.Combine(root, "runtimes", "win-x64", "native"), "maa_native_missing");
            string resource = LocalDirectory(Path.Combine(root, "resource"), "maa_resource_missing");
            string agent = LocalDirectory(Path.Combine(root, "MaaAgentBinary"), "maa_agent_missing");
            LocalFile(Path.Combine(native, "MaaFramework.dll"), tool["maaFrameworkSha256"] as string, "maa_framework_changed");
            if (!String.Equals(TreeHash(native), tool["nativeTreeSha256"] as string, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("maa_native_tree_changed");
            if (!String.Equals(TreeHash(agent), tool["agentTreeSha256"] as string, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("maa_agent_tree_changed");
            if (!String.Equals(TreeHash(resource), tool["resourceTreeSha256"] as string, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("maa_resource_tree_changed");

            string adbPath = null;
            string adbSerial = null;
            CznProfile profile = null;
            if (requireManagerBinding)
            {
                string managerPath = Environment.GetEnvironmentVariable("YEYU_GAMER_INSTALLATION_BINDING_PATH");
                string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
                string full = String.IsNullOrWhiteSpace(managerPath) ? String.Empty : Path.GetFullPath(managerPath);
                string stagingRoot = String.IsNullOrWhiteSpace(staging) ? String.Empty : Path.GetFullPath(staging).TrimEnd('\\') + "\\";
                if (String.IsNullOrEmpty(full) || String.IsNullOrEmpty(stagingRoot) || !full.StartsWith(stagingRoot, StringComparison.OrdinalIgnoreCase) ||
                    Path.GetFileName(full) != "installation-binding.json" || !File.Exists(full))
                    throw new InvalidOperationException("manager_installation_binding_invalid");
                IDictionary<string, object> manager = Json.DeserializeObject(File.ReadAllText(full, Encoding.UTF8)) as IDictionary<string, object>;
                int expectedCount = GameId == "CZN" ? 6 : 5;
                if (manager == null || manager.Count != expectedCount || !NumberEquals(manager["schemaVersion"], 3) ||
                    manager["gameId"] as string != GameId || !(manager["toolPath"] is string) ||
                    (GameId == "CZN" && !(manager["dailyTaskProfile"] is IDictionary<string, object>)) || !(manager["emulatorBinding"] is IDictionary<string, object>))
                    throw new InvalidOperationException("manager_installation_binding_invalid");
                string configuredRoot = LocalDirectory(manager["toolPath"] as string, "configured_tool_missing");
                if (!String.Equals(root, configuredRoot, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("configured_tool_differs_from_package");
                IDictionary<string, object> emulator = manager["emulatorBinding"] as IDictionary<string, object>;
                if (emulator.Count != 6 || emulator["provider"] as string != "ldplayer" ||
                    !NumberEquals(emulator["instanceIndex"], 0) || emulator["instanceName"] as string != "雷电模拟器" ||
                    emulator["adbSerial"] as string != "emulator-5554" || !(emulator["adbPath"] is string) || !(emulator["consolePath"] is string))
                    throw new InvalidOperationException("emulator_binding_invalid");
                adbPath = LocalFile(emulator["adbPath"] as string, null, "configured_adb_missing");
                LocalFile(emulator["consolePath"] as string, null, "configured_ldconsole_missing");
                adbSerial = emulator["adbSerial"] as string;
                if (GameId == "CZN") profile = ParseProfile(manager["dailyTaskProfile"] as IDictionary<string, object>);
            }
            return new ToolBinding { Root = root, NativeDirectory = native, ResourceDirectory = resource, AgentDirectory = agent, AdbPath = adbPath, AdbSerial = adbSerial, Profile = profile };
        }

        private static void EnsureFormalGui(ToolBinding binding)
        {
            string gui = LocalFile(Path.Combine(binding.Root, "MFAAvalonia.exe"), null, "formal_gui_missing");
            bool running = Process.GetProcessesByName("MFAAvalonia").Any(value =>
            {
                try { return String.Equals(Path.GetFullPath(value.MainModule.FileName), gui, StringComparison.OrdinalIgnoreCase); }
                catch { return false; }
            });
            if (!running) Process.Start(new ProcessStartInfo(gui) { WorkingDirectory = binding.Root, UseShellExecute = true });
            DateTime deadline = DateTime.UtcNow.AddSeconds(90);
            while (DateTime.UtcNow < deadline)
            {
                if (Process.GetProcessesByName("MFAAvalonia").Any(value =>
                {
                    try { return value.MainWindowHandle != IntPtr.Zero && String.Equals(Path.GetFullPath(value.MainModule.FileName), gui, StringComparison.OrdinalIgnoreCase); }
                    catch { return false; }
                })) return;
                System.Threading.Thread.Sleep(500);
            }
            throw new InvalidOperationException("formal_gui_window_timeout");
        }

        private static int ProbeBinding()
        {
            try
            {
                ToolBinding binding = LoadBinding(false);
                Console.WriteLine(Json.Serialize(new Dictionary<string, object>
                {
                    { "ok", true }, { "processStarted", false }, { "gameId", GameId },
                    { "toolRoot", binding.Root }, { "resourceTreeSha256", TreeHash(binding.ResourceDirectory) },
                    { "supportedOperations", Operations.Keys.OrderBy(value => value, StringComparer.Ordinal).ToArray() }
                }));
                return 0;
            }
            catch (Exception error)
            {
                Console.WriteLine(Json.Serialize(new Dictionary<string, object> { { "ok", false }, { "processStarted", false }, { "reason", SafeDetail(error.Message) } }));
                return 78;
            }
        }

        private static int ValidateProfile()
        {
            try
            {
                IDictionary<string, object> profileDocument = Json.DeserializeObject(Console.In.ReadToEnd()) as IDictionary<string, object>;
                CznProfile profile = ParseProfile(profileDocument);
                string compiled = BuildStaminaOverride(profile);
                Console.WriteLine(Json.Serialize(new Dictionary<string, object>
                {
                    { "ok", true }, { "profile", profile.ToDocument() }, { "pipelineOverrideSha256", HashBytes(Encoding.UTF8.GetBytes(compiled)) },
                    { "pipelineOverride", Json.DeserializeObject(compiled) }
                }));
                return 0;
            }
            catch (Exception error)
            {
                Console.WriteLine(Json.Serialize(new Dictionary<string, object> { { "ok", false }, { "reason", SafeDetail(error.Message) } }));
                return 64;
            }
        }

        private static Dictionary<string, string> ParseArguments(string[] args)
        {
            if (args == null || args.Length != 8) return null;
            var result = new Dictionary<string, string>(StringComparer.Ordinal);
            for (int index = 0; index < args.Length; index += 2)
            {
                if (!args[index].StartsWith("--", StringComparison.Ordinal) || result.ContainsKey(args[index])) return null;
                result.Add(args[index], args[index + 1]);
            }
            return result.Count == 4 && result.ContainsKey("--protocol-version") && result.ContainsKey("--run-id") &&
                result.ContainsKey("--run-attempt-id") && result.ContainsKey("--game-id") ? result : null;
        }

        private static bool MatchesRequest(IDictionary<string, object> request, IDictionary<string, string> options)
        {
            return request != null && request["protocolVersion"] as string == ProtocolVersion && request["gameId"] as string == GameId &&
                request["runId"] as string == options["--run-id"] && request["runAttemptId"] as string == options["--run-attempt-id"] &&
                request["preserveClientOnStop"] is bool && (bool)request["preserveClientOnStop"];
        }

        private static int TerminalSetupFailure(IDictionary<string, object> request, object[] ids, string reason)
        {
            if (request == null || ids == null) return 64;
            Emit(Base(request, "hello", 0, new Dictionary<string, object>
            {
                { "packageId", PackageId }, { "packageVersion", PackageVersion() }, { "packageDigest", PackageDigest() },
                { "runnerPid", Process.GetCurrentProcess().Id }, { "acceptedTodoInstanceIds", ids }
            }));
            string[] unresolved = ids.Cast<string>().ToArray();
            Console.Error.WriteLine("installation_binding_invalid:" + SafeDetail(reason));
            Emit(Base(request, "run_terminal", 1, new Dictionary<string, object>
            {
                { "status", "failed" }, { "transportOutcome", "clean" }, { "attemptedTodoInstanceIds", new string[0] },
                { "completedTodoInstanceIds", new string[0] }, { "unresolvedTodoInstanceIds", ids },
                { "terminalEventDigest", TerminalDigest(request, "failed", new string[0], new string[0], unresolved, 0) }, { "exitCode", 0 }
            }));
            return 0;
        }

        private static IDictionary<string, object> Base(IDictionary<string, object> request, string type, int sequence, IDictionary<string, object> fields)
        {
            var result = new Dictionary<string, object>
            {
                { "schemaVersion", 1 }, { "protocolVersion", ProtocolVersion }, { "eventType", type }, { "sequence", sequence },
                { "runId", request["runId"] }, { "runAttemptId", request["runAttemptId"] }, { "gameId", GameId },
                { "fencingToken", request["fencingToken"] }, { "at", DateTime.UtcNow.ToString("o") }
            };
            foreach (KeyValuePair<string, object> pair in fields) result[pair.Key] = pair.Value;
            return result;
        }

        private static void Emit(IDictionary<string, object> value) { Console.Out.WriteLine(Json.Serialize(value)); Console.Out.Flush(); }
        private static bool NumberEquals(object value, int expected) { return value is int && (int)value == expected; }
        private static string PackageVersion() { return FileVersionInfo.GetVersionInfo(Process.GetCurrentProcess().MainModule.FileName).FileVersion ?? "0.0.0"; }
        private static string PackageDigest() { try { string root=AppDomain.CurrentDomain.BaseDirectory; byte[] raw=File.ReadAllBytes(Path.Combine(root,"install-manifest.json")); var document=Json.DeserializeObject(Encoding.UTF8.GetString(raw)) as IDictionary<string,object>; object[] files=document["files"] as object[]; var entries=new SortedDictionary<string,string>(StringComparer.Ordinal); foreach(object item in files){var file=item as IDictionary<string,object>;entries.Add(file["path"] as string,file["sha256"] as string);} using(var sha=SHA256.Create()) using(var stream=new MemoryStream()){stream.Write(raw,0,raw.Length);foreach(var entry in entries){byte[] name=Encoding.UTF8.GetBytes(entry.Key);byte[] digest=Encoding.ASCII.GetBytes(entry.Value);stream.Write(name,0,name.Length);stream.Write(digest,0,digest.Length);}return "sha256:"+ToHex(sha.ComputeHash(stream.ToArray()));}} catch(Exception error){throw new InvalidDataException("package_digest_unavailable",error);} }
        private static string TerminalDigest(IDictionary<string, object> request, string status, string[] attempted, string[] completed, string[] unresolved, int exitCode) { string payload=(request["runId"] as string)+"\n"+(request["runAttemptId"] as string)+"\n"+status+"\n"+String.Join("\n",attempted)+"\n--completed--\n"+String.Join("\n",completed)+"\n--unresolved--\n"+String.Join("\n",unresolved)+"\n"+exitCode; return "sha256:"+HashBytes(Encoding.UTF8.GetBytes(payload)); }
        private static string SafeDetail(string value) { return String.IsNullOrWhiteSpace(value) ? "unspecified" : value.Replace('\r', ' ').Replace('\n', ' ').Trim(); }
        private static string Hash(string path) { using (SHA256 sha = SHA256.Create()) using (FileStream stream = File.OpenRead(path)) return ToHex(sha.ComputeHash(stream)); }
        private static string HashBytes(byte[] value) { using (SHA256 sha = SHA256.Create()) return ToHex(sha.ComputeHash(value)); }
        private static string ToHex(byte[] value) { return String.Concat(value.Select(item => item.ToString("x2")).ToArray()); }

        private static string TreeHash(string root)
        {
            var builder = new StringBuilder();
            string prefix = Path.GetFullPath(root).TrimEnd('\\') + "\\";
            foreach (string file in Directory.GetFiles(root, "*", SearchOption.AllDirectories).OrderBy(value => value, StringComparer.OrdinalIgnoreCase))
            {
                string relative = Path.GetFullPath(file).Substring(prefix.Length).Replace('\\', '/');
                builder.Append(relative).Append('|').Append(new FileInfo(file).Length).Append('|').Append(Hash(file)).Append('\n');
            }
            return HashBytes(Encoding.UTF8.GetBytes(builder.ToString()));
        }

        private static string LocalDirectory(string value, string reason)
        {
            if (String.IsNullOrWhiteSpace(value)) throw new InvalidOperationException(reason);
            string full = Path.GetFullPath(value).TrimEnd('\\');
            if (full.StartsWith("\\\\", StringComparison.Ordinal) || !Directory.Exists(full) || (File.GetAttributes(full) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidOperationException(reason);
            return full;
        }

        private static string LocalFile(string value, string expectedHash, string reason)
        {
            if (String.IsNullOrWhiteSpace(value)) throw new InvalidOperationException(reason);
            string full = Path.GetFullPath(value);
            if (full.StartsWith("\\\\", StringComparison.Ordinal) || !File.Exists(full) || (File.GetAttributes(full) & FileAttributes.ReparsePoint) != 0 ||
                (!String.IsNullOrWhiteSpace(expectedHash) && !String.Equals(Hash(full), expectedHash, StringComparison.OrdinalIgnoreCase)))
                throw new InvalidOperationException(reason);
            return full;
        }

        private static class Native
        {
            [UnmanagedFunctionPointer(CallingConvention.StdCall)]
            public delegate void EventCallback(IntPtr handle, IntPtr message, IntPtr detail, IntPtr transArg);

            [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            public static extern bool SetDllDirectory(string path);

            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] private static extern IntPtr MaaAdbControllerCreate(IntPtr adbPath, IntPtr address, UInt64 screencapMethods, UInt64 inputMethods, IntPtr config, IntPtr agentPath);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] public static extern void MaaControllerDestroy(IntPtr controller);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] public static extern long MaaControllerPostConnection(IntPtr controller);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] public static extern int MaaControllerWait(IntPtr controller, long id);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] public static extern IntPtr MaaResourceCreate();
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] public static extern void MaaResourceDestroy(IntPtr resource);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] private static extern long MaaResourcePostBundle(IntPtr resource, IntPtr path);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] public static extern int MaaResourceWait(IntPtr resource, long id);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] public static extern IntPtr MaaTaskerCreate();
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] public static extern void MaaTaskerDestroy(IntPtr tasker);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] [return: MarshalAs(UnmanagedType.I1)] public static extern bool MaaTaskerBindResource(IntPtr tasker, IntPtr resource);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] [return: MarshalAs(UnmanagedType.I1)] public static extern bool MaaTaskerBindController(IntPtr tasker, IntPtr controller);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] [return: MarshalAs(UnmanagedType.I1)] public static extern bool MaaTaskerInited(IntPtr tasker);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] public static extern long MaaTaskerAddSink(IntPtr tasker, EventCallback callback, IntPtr transArg);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] public static extern void MaaTaskerRemoveSink(IntPtr tasker, long sinkId);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] private static extern long MaaTaskerPostTask(IntPtr tasker, IntPtr entry, IntPtr pipelineOverride);
            [DllImport("MaaFramework.dll", CallingConvention = CallingConvention.Cdecl)] public static extern int MaaTaskerWait(IntPtr tasker, long id);

            public static IntPtr CreateAdbController(string adbPath, string address, UInt64 screencapMethods, UInt64 inputMethods, string config, string agentPath)
            {
                return WithUtf8(new[] { adbPath, address, config, agentPath }, values => MaaAdbControllerCreate(values[0], values[1], screencapMethods, inputMethods, values[2], values[3]));
            }

            public static long ResourcePostBundle(IntPtr resource, string path) { return WithUtf8(new[] { path }, values => MaaResourcePostBundle(resource, values[0])); }
            public static long TaskerPostTask(IntPtr tasker, string entry, string pipelineOverride) { return WithUtf8(new[] { entry, pipelineOverride }, values => MaaTaskerPostTask(tasker, values[0], values[1])); }

            private static T WithUtf8<T>(string[] values, Func<IntPtr[], T> action)
            {
                var pointers = new IntPtr[values.Length];
                try
                {
                    for (int index = 0; index < values.Length; index++)
                    {
                        byte[] bytes = Encoding.UTF8.GetBytes(values[index] + "\0");
                        pointers[index] = Marshal.AllocHGlobal(bytes.Length);
                        Marshal.Copy(bytes, 0, pointers[index], bytes.Length);
                    }
                    return action(pointers);
                }
                finally { foreach (IntPtr pointer in pointers) if (pointer != IntPtr.Zero) Marshal.FreeHGlobal(pointer); }
            }

            public static string Utf8String(IntPtr pointer)
            {
                if (pointer == IntPtr.Zero) return String.Empty;
                int length = 0;
                while (Marshal.ReadByte(pointer, length) != 0 && length < 1048576) length++;
                byte[] bytes = new byte[length];
                Marshal.Copy(pointer, bytes, 0, length);
                return Encoding.UTF8.GetString(bytes);
            }
        }
    }
}
