using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;

namespace YeYuGamer.NteAdapter
{
    /// <summary>
    /// Fixed adapter for the locally patched ok-nte DailyTask.
    ///
    /// The Manager supplies only a run-scoped selection and its persisted local
    /// installation binding. Commands, arguments and paths are never accepted
    /// from the API request. The package binding pins the one supported local
    /// source tree and the selected-stage hook by SHA-256.
    /// </summary>
    internal static class Program
    {
        private const string PackageId = "legacy-night-rain-gamer";
        private const string ProtocolVersion = "1.1";
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        private static readonly IDictionary<string, string> UpstreamOperations =
            new Dictionary<string, string>(StringComparer.Ordinal)
            {
                { "attach-home", "attach-world" },
                { "mail", "claim-mail" },
                { "daily-activity", "inspect-daily-progress" },
                { "spend-city-vitality", "spend-urban-vitality" },
                { "claim-activity-reward", "claim-daily-reward" },
                { "claim-cycle-reward", "claim-period-reward" }
            };

        private sealed class ToolBinding
        {
            public string Root;
            public string FormalLauncher;
            public string UpdateState;
            public string Pythonw;
            public string GuiEntry;
            public string DailyTask;
            public NteProfile Profile;
        }

        private sealed class NteProfile
        {
            public string AnomalyTaskType;
            public string ExpRewardTarget;
            public int MaterialIndex;
            public int StaminaTarget;
            public bool AutoCycleSubTask;
            public string CoffeeMode;

            public IDictionary<string, object> ToDocument()
            {
                return new Dictionary<string, object>
                {
                    { "anomalyTaskType", AnomalyTaskType },
                    { "expRewardTarget", ExpRewardTarget },
                    { "materialIndex", MaterialIndex },
                    { "staminaTarget", StaminaTarget },
                    { "autoCycleSubTask", AutoCycleSubTask },
                    { "coffeeMode", CoffeeMode }
                };
            }
        }

        public static int Main(string[] args)
        {
            if (args != null && args.Length == 1 && args[0] == "--probe-binding")
                return ProbeBinding();

            Dictionary<string, string> options = ParseArguments(args);
            if (options == null || options["--protocol-version"] != ProtocolVersion ||
                options["--game-id"] != "NTE") return 64;

            IDictionary<string, object> request;
            try { request = Json.DeserializeObject(Console.In.ReadLine() ?? String.Empty) as IDictionary<string, object>; }
            catch { return 64; }
            if (!MatchesRequest(request, options)) return 64;

            object[] ids = request["executableTodoInstanceIds"] as object[];
            object[] todos = request["todos"] as object[];
            if (ids == null || todos == null || ids.Length == 0 || ids.Length != todos.Length) return 64;

            ToolBinding binding;
            try { binding = LoadBinding(true); }
            catch (Exception error) { return TerminalSetupFailure(request, ids, error.Message); }

            string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            if (String.IsNullOrWhiteSpace(staging) || !Directory.Exists(staging))
                return TerminalSetupFailure(request, ids, "adapter_staging_missing");

            var managerByUpstream = new Dictionary<string, string>(StringComparer.Ordinal);
            var todoByManager = new Dictionary<string, Tuple<string, IDictionary<string, object>>>(StringComparer.Ordinal);
            var attempts = new Dictionary<string, string>(StringComparer.Ordinal);
            for (int index = 0; index < ids.Length; index++)
            {
                string id = ids[index] as string;
                IDictionary<string, object> todo = todos[index] as IDictionary<string, object>;
                string operation = todo == null ? null : todo["operation"] as string;
                string upstream;
                if (String.IsNullOrWhiteSpace(id) || String.IsNullOrWhiteSpace(operation) ||
                    !UpstreamOperations.TryGetValue(operation, out upstream) ||
                    todoByManager.ContainsKey(operation) || managerByUpstream.ContainsKey(upstream)) return 64;
                todoByManager.Add(operation, Tuple.Create(id, todo));
                managerByUpstream.Add(upstream, operation);
                attempts.Add(id, Guid.NewGuid().ToString());
            }

            Emit(Base(request, "hello", 0, new Dictionary<string, object>
            {
                { "packageId", PackageId }, { "packageVersion", PackageVersion() },
                { "packageDigest", PackageDigest() }, { "runnerPid", Process.GetCurrentProcess().Id },
                { "acceptedTodoInstanceIds", ids }
            }));

            int sequence = 1;
            var attempted = new HashSet<string>(StringComparer.Ordinal);
            var terminal = new HashSet<string>(StringComparer.Ordinal);
            var completed = new HashSet<string>(StringComparer.Ordinal);
            var cancellationRequested = new ManualResetEvent(false);
            var controlReader = new Thread(delegate()
            {
                try
                {
                    string line;
                    while ((line = Console.In.ReadLine()) != null)
                    {
                        IDictionary<string, object> control = Json.DeserializeObject(line) as IDictionary<string, object>;
                        if (MatchesCancel(control, request)) { cancellationRequested.Set(); return; }
                    }
                }
                catch { }
            });
            controlReader.IsBackground = true;
            controlReader.Start();

            Action<string> startTodo = managerOperation =>
            {
                Tuple<string, IDictionary<string, object>> target;
                if (!todoByManager.TryGetValue(managerOperation, out target) || !attempted.Add(target.Item1)) return;
                Emit(Base(request, "todo_attempt_started", sequence++, new Dictionary<string, object>
                {
                    { "todoInstanceId", target.Item1 }, { "todoAttemptId", attempts[target.Item1] },
                    { "attemptNo", AttemptNumber(target.Item2) }, { "operation", managerOperation }
                }));
            };

            Action<string, string, string> finishTodo = (managerOperation, state, detail) =>
            {
                Tuple<string, IDictionary<string, object>> target;
                if (!todoByManager.TryGetValue(managerOperation, out target) || terminal.Contains(target.Item1)) return;
                if (!attempted.Contains(target.Item1)) return;
                string id = target.Item1;
                string attempt = attempts[id];
                string fileName = "nte-daily-" + Guid.NewGuid().ToString("N") + ".txt";
                string artifactPath = Path.Combine(staging, fileName);
                string safeDetail = SafeDetail(detail);
                if (String.IsNullOrWhiteSpace(safeDetail))
                    safeDetail = managerOperation + " upstream stage " + state;
                string evidence = "managerOperation=" + managerOperation + "; upstreamState=" + state +
                    "; detail=" + safeDetail;
                File.WriteAllText(artifactPath, evidence, new UTF8Encoding(false));
                string artifactId = Guid.NewGuid().ToString();
                Emit(Base(request, "artifact_staged", sequence++, new Dictionary<string, object>
                {
                    { "todoInstanceId", id }, { "todoAttemptId", attempt }, { "artifactId", artifactId },
                    { "kind", "tool-log-outcome" }, { "fileName", fileName }, { "mimeType", "text/plain" },
                    { "sizeBytes", new FileInfo(artifactPath).Length }, { "sha256", Hash(artifactPath) },
                    { "capturedAt", DateTime.UtcNow.ToString("o") }
                }));

                string status = state == "completed" ? "completed" : state == "skipped" ? "skipped" :
                    state == "failed" ? "blocked" : "review_required";
                string reasonCode = state == "completed" ? "upstream_stage_completed" :
                    state == "skipped" ? "upstream_stage_not_needed" :
                    state == "failed" ? TypedFailureReason(safeDetail, "upstream_stage_failed") : "upstream_stage_unverified";
                Emit(Base(request, "todo_terminal", sequence++, new Dictionary<string, object>
                {
                    { "todoInstanceId", id }, { "todoAttemptId", attempt }, { "status", status },
                    { "reasonCode", reasonCode }, { "reason", safeDetail },
                    { "retryable", state == "failed" }, { "evidenceArtifactIds", new string[] { artifactId } }
                }));
                terminal.Add(id);
                if (status == "completed") completed.Add(id);
            };

            int toolExitCode;
            string transportDetail;
            bool cancelled;
            try
            {
                // Launcher/update/readiness belongs to the attach attempt.  A
                // downstream daily operation starts only when the run-scoped
                // bridge reports its own `started` stage.
                startTodo("attach-home");
                toolExitCode = RunTool(binding, managerByUpstream, (upstream, state, detail) =>
                {
                    string managerOperation;
                    if (!managerByUpstream.TryGetValue(upstream, out managerOperation)) return;
                    if (state == "started") startTodo(managerOperation);
                    else if (state == "completed" || state == "failed" || state == "skipped")
                        finishTodo(managerOperation, state, detail);
                }, delegate { return cancellationRequested.WaitOne(0); }, out transportDetail, out cancelled);
            }
            catch (Exception error)
            {
                toolExitCode = -1;
                transportDetail = TypedStartupFailure(error);
                cancelled = false;
            }

            if (!cancelled) foreach (string operation in todoByManager.Keys)
            {
                string id = todoByManager[operation].Item1;
                if (attempted.Contains(id) && !terminal.Contains(id))
                    finishTodo(operation, toolExitCode == 0 ? "review_required" : "failed",
                        transportDetail + ";stage_event_missing");
            }

            bool allCompleted = toolExitCode == 0 && completed.Count == ids.Length;
            string runStatus = cancelled ? "cancelled" : allCompleted ? "completed" : toolExitCode == 0 ? "review_required" : "failed";
            string transportOutcome = cancelled ? "cancelled" : toolExitCode == 0 ? "clean" : "crashed";
            string[] attemptedIds = ids.Cast<string>().Where(attempted.Contains).ToArray();
            string[] completedIds = ids.Cast<string>().Where(completed.Contains).ToArray();
            string[] unresolvedIds = ids.Cast<string>().Where(id => !completed.Contains(id)).ToArray();
            Emit(Base(request, "run_terminal", sequence, new Dictionary<string, object>
            {
                { "status", runStatus }, { "transportOutcome", transportOutcome },
                { "attemptedTodoInstanceIds", attemptedIds },
                { "completedTodoInstanceIds", completedIds },
                { "unresolvedTodoInstanceIds", unresolvedIds },
                { "terminalEventDigest", TerminalDigest(request, runStatus, attemptedIds, completedIds, unresolvedIds, 0) }, { "exitCode", 0 }
            }));
            return 0;
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
            return result.Count == 4 && result.ContainsKey("--protocol-version") &&
                result.ContainsKey("--run-id") && result.ContainsKey("--run-attempt-id") &&
                result.ContainsKey("--game-id") ? result : null;
        }

        private static bool MatchesRequest(IDictionary<string, object> request, IDictionary<string, string> options)
        {
            return request != null && request["protocolVersion"] as string == ProtocolVersion &&
                request["gameId"] as string == "NTE" && request["runId"] as string == options["--run-id"] &&
                request["runAttemptId"] as string == options["--run-attempt-id"] &&
                request["preserveClientOnStop"] is bool && (bool)request["preserveClientOnStop"];
        }

        private static bool MatchesCancel(IDictionary<string, object> control, IDictionary<string, object> request)
        {
            return control != null && control["schemaVersion"] is int && (int)control["schemaVersion"] == 1 &&
                control["protocolVersion"] as string == ProtocolVersion && control["controlType"] as string == "cancel" &&
                control["runId"] as string == request["runId"] as string &&
                control["runAttemptId"] as string == request["runAttemptId"] as string &&
                control["fencingToken"] as string == request["fencingToken"] as string &&
                control["reasonCode"] is string;
        }

        private static ToolBinding LoadBinding(bool requireManagerBinding)
        {
            string packageRoot = AppDomain.CurrentDomain.BaseDirectory;
            string packagePath = Path.Combine(packageRoot, "tool-binding.json");
            IDictionary<string, object> package = Json.DeserializeObject(File.ReadAllText(packagePath, Encoding.UTF8)) as IDictionary<string, object>;
            IDictionary<string, object> tool = package == null ? null : package["tool"] as IDictionary<string, object>;
            if (package == null || tool == null || !NumberEquals(package["schemaVersion"], 1) ||
                package["gameId"] as string != "NTE") throw new InvalidOperationException("package_binding_invalid");

            string root = LocalDirectory(tool["root"] as string, "configured_tool_missing");
            NteProfile profile = null;
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
                if (manager == null || manager.Count != 5 || !NumberEquals(manager["schemaVersion"], 2) ||
                    manager["gameId"] as string != "NTE" || !(manager["gamePath"] is string) ||
                    !(manager["toolPath"] is string) || !(manager["dailyTaskProfile"] is IDictionary<string, object>))
                    throw new InvalidOperationException("manager_installation_binding_invalid");
                string configuredRoot = LocalDirectory(manager["toolPath"] as string, "configured_tool_missing");
                string gamePath = LocalFile(manager["gamePath"] as string, null, "configured_game_missing");
                if (!String.Equals(root, configuredRoot, StringComparison.OrdinalIgnoreCase) || String.IsNullOrEmpty(gamePath))
                    throw new InvalidOperationException("configured_tool_differs_from_package");
                profile = ParseProfile(manager["dailyTaskProfile"] as IDictionary<string, object>);
            }

            return new ToolBinding
            {
                Root = root,
                FormalLauncher = LocalFile(Path.Combine(root, "ok-nte.exe"), tool["formalLauncherSha256"] as string, "configured_formal_gui_missing"),
                UpdateState = LocalFile(Path.Combine(root, "data", "apps", "ok-nte", "app.json"), null, "configured_update_state_missing"),
                Pythonw = LocalFile(Path.Combine(root, "data", "apps", "ok-nte", "python", "pythonw.exe"), null, "configured_gui_python_missing"),
                GuiEntry = LocalFile(Path.Combine(root, "data", "apps", "ok-nte", "working", "main.py"), null, "configured_gui_entry_missing"),
                DailyTask = LocalFile(Path.Combine(root, "data", "apps", "ok-nte", "working", "src", "tasks", "DailyTask.py"), null, "configured_daily_task_missing"),
                Profile = profile
            };
        }

        private static NteProfile ParseProfile(IDictionary<string, object> value)
        {
            if (value == null || value.Count != 6 ||
                !(value["anomalyTaskType"] is string) || !(value["expRewardTarget"] is string) ||
                !(value["materialIndex"] is int) || !(value["staminaTarget"] is int) ||
                !(value["autoCycleSubTask"] is bool) || !(value["coffeeMode"] is string))
                throw new InvalidOperationException("nte_daily_profile_invalid");

            string taskType = value["anomalyTaskType"] as string;
            string expTarget = value["expRewardTarget"] as string;
            int materialIndex = (int)value["materialIndex"];
            int staminaTarget = (int)value["staminaTarget"];
            string coffeeMode = value["coffeeMode"] as string;
            string[] taskTypes = { "经验与甲硬币", "异能升级材料", "弧盘突破材料", "空幕" };
            string[] expTargets = { "角色经验", "弧盘经验", "甲硬币" };
            string[] coffeeModes = { "不执行", "领取/补货", "完整自动化" };
            int maximumMaterialIndex = taskType == "空幕" ? 6 : 5;
            if (!taskTypes.Contains(taskType) || !expTargets.Contains(expTarget) ||
                materialIndex < 1 || materialIndex > 6 ||
                ((taskType == "异能升级材料" || taskType == "弧盘突破材料" || taskType == "空幕") &&
                    materialIndex > maximumMaterialIndex) ||
                staminaTarget < 40 || staminaTarget > 360 || staminaTarget % 40 != 0 ||
                !coffeeModes.Contains(coffeeMode))
                throw new InvalidOperationException("nte_daily_profile_invalid");
            if (coffeeMode != "不执行")
                throw new InvalidOperationException("nte_coffee_mode_requires_review");

            return new NteProfile
            {
                AnomalyTaskType = taskType,
                ExpRewardTarget = expTarget,
                MaterialIndex = materialIndex,
                StaminaTarget = staminaTarget,
                AutoCycleSubTask = (bool)value["autoCycleSubTask"],
                CoffeeMode = coffeeMode
            };
        }

        private static int RunTool(ToolBinding binding, IDictionary<string, string> managerByUpstream,
            Action<string, string, string> onStage, Func<bool> cancellationRequested, out string detail, out bool cancelled)
        {
            cancelled = false;
            string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            string stageFile = Path.Combine(staging, "nte-stage-" + Guid.NewGuid().ToString("N") + ".jsonl");
            File.WriteAllText(stageFile, String.Empty, new UTF8Encoding(false));
            string bridgePath = LocalFile(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "NteYeYuBridge.py"), null, "nte_bridge_missing");
            string originalMain = null;
            bool originalAutoStart;
            bool originalAdmin;
            ReadFormalMode(binding, out originalAutoStart, out originalAdmin);
            DateTimeOffset formalStartedAt = DateTimeOffset.UtcNow;
            Process firstFormal = null;
            Process executionFormal = null;
            Process inner = null;
            var seen = new HashSet<string>(StringComparer.Ordinal);
            try
            {
                if (FindFormalProcesses(binding, DateTimeOffset.MinValue).Count > 0)
                    throw new InvalidOperationException("ok_nte_formal_gui_busy");

                // First pass is the user's real formal GUI/update entry.  It
                // must visibly reach an idle, current state before any bridge
                // is injected into the freshly updated working tree.
                firstFormal = StartFormal(binding, null, null, null, null);
                WaitForFormalReady(binding, formalStartedAt, TimeSpan.FromMinutes(5), cancellationRequested);
                StopFormalProcesses(binding, formalStartedAt, cancellationRequested);
                ThrowIfCancellationRequested(cancellationRequested);

                // The formal first pass is allowed to update its managed
                // working tree.  Capture and patch only the freshly updated
                // entrypoint, never the pre-update copy.
                originalMain = File.ReadAllText(binding.GuiEntry, Encoding.UTF8);
                PatchFormalMain(binding.GuiEntry, originalMain, bridgePath);
                SetFormalMode(binding, true, false);

                DateTimeOffset executionStartedAt = DateTimeOffset.UtcNow;
                executionFormal = StartFormal(
                    binding,
                    stageFile,
                    Json.Serialize(managerByUpstream.Keys.ToArray()),
                    Json.Serialize(binding.Profile.ToDocument()),
                    Path.GetDirectoryName(bridgePath));
                inner = WaitForInner(binding, executionStartedAt, TimeSpan.FromMinutes(3), cancellationRequested);
                while (!inner.WaitForExit(150))
                {
                    if (cancellationRequested != null && cancellationRequested()) throw new OperationCanceledException("cancelled_by_manager");
                    DrainStageEvents(stageFile, seen, onStage);
                }
                inner.WaitForExit();
                DrainStageEvents(stageFile, seen, onStage);
                if (seen.Count == 0)
                {
                    detail = "telemetry_missing: ok-nte DailyTask emitted no run-scoped stage event";
                    return 73;
                }
                string version = ReadFormalVersion(binding);
                detail = "formalGui=ok-nte.exe;formalUpdate=checked;version=" + version +
                    ";fixedTask=DailyTask;toolExitCode=" + inner.ExitCode;
                return inner.ExitCode;
            }
            catch (OperationCanceledException)
            {
                cancelled = true;
                detail = "cancelled_by_manager";
                return 0;
            }
            finally
            {
                if (originalMain != null)
                    try { File.WriteAllText(binding.GuiEntry, originalMain, new UTF8Encoding(false)); } catch { }
                try { SetFormalMode(binding, originalAutoStart, originalAdmin); } catch { }
                try { StopFormalProcesses(binding, formalStartedAt, cancellationRequested, inner); } catch { }
                if (inner != null) inner.Dispose();
                if (firstFormal != null) firstFormal.Dispose();
                if (executionFormal != null) executionFormal.Dispose();
            }
        }

        private static Process StartFormal(ToolBinding binding, string stageFile, string selected, string profile, string bridgeRoot)
        {
            var start = new ProcessStartInfo
            {
                FileName = binding.FormalLauncher,
                WorkingDirectory = binding.Root,
                UseShellExecute = false,
                CreateNoWindow = false,
                WindowStyle = ProcessWindowStyle.Normal
            };
            if (!String.IsNullOrEmpty(stageFile)) start.EnvironmentVariables["YEYU_GAMER_STAGE_FILE"] = stageFile;
            if (!String.IsNullOrEmpty(selected)) start.EnvironmentVariables["YEYU_GAMER_SELECTED_OPERATIONS"] = selected;
            if (!String.IsNullOrEmpty(profile)) start.EnvironmentVariables["YEYU_GAMER_NTE_PROFILE"] = profile;
            if (!String.IsNullOrEmpty(bridgeRoot)) start.EnvironmentVariables["YEYU_GAMER_NTE_BRIDGE_ROOT"] = bridgeRoot;
            var process = new Process { StartInfo = start };
            if (!process.Start()) throw new InvalidOperationException("ok_nte_formal_gui_not_started");
            return process;
        }

        private static void WaitForFormalReady(ToolBinding binding, DateTimeOffset startedAt, TimeSpan timeout, Func<bool> cancellationRequested)
        {
            DateTimeOffset deadline = DateTimeOffset.UtcNow.Add(timeout);
            DateTimeOffset stableSince = DateTimeOffset.MinValue;
            bool splashLikeWindowSeen = false;
            while (DateTimeOffset.UtcNow < deadline)
            {
                if (cancellationRequested != null && cancellationRequested()) throw new OperationCanceledException("cancelled_by_manager");
                List<Process> formalProcesses = FindFormalProcesses(binding, startedAt);
                bool visible = false;
                bool interactive = false;
                foreach (Process process in formalProcesses)
                {
                    try
                    {
                        IntPtr window = process.HasExited ? IntPtr.Zero : process.MainWindowHandle;
                        visible = visible || window != IntPtr.Zero;
                        if (window != IntPtr.Zero)
                        {
                            bool splashLike;
                            bool windowInteractive = IsInteractiveFormalWindow(window, cancellationRequested, out splashLike);
                            interactive = interactive || windowInteractive;
                            splashLikeWindowSeen = splashLikeWindowSeen || splashLike;
                        }
                    }
                    catch (OperationCanceledException) { throw; }
                    catch (TimeoutException) { throw; }
                    catch { }
                    finally { process.Dispose(); }
                }
                bool ready = false;
                string updateError = null;
                try
                {
                    IDictionary<string, object> state = Json.DeserializeObject(File.ReadAllText(binding.UpdateState, Encoding.UTF8)) as IDictionary<string, object>;
                    updateError = state == null ? null : state["update_error"] as string;
                    ready = state != null && state["update_state"] as string == "idle" &&
                        !String.IsNullOrWhiteSpace(state["current_version"] as string) && state["update_error"] == null;
                }
                catch { }
                if (!String.IsNullOrWhiteSpace(updateError)) throw new InvalidOperationException("client_update_required:" + updateError);
                // A process, HWND, logo/splash, and idle app.json are transport
                // observations. They are not a usable application surface.
                if (visible && interactive && ready)
                {
                    if (stableSince == DateTimeOffset.MinValue) stableSince = DateTimeOffset.UtcNow;
                    if (DateTimeOffset.UtcNow - stableSince >= TimeSpan.FromSeconds(5)) return;
                }
                else stableSince = DateTimeOffset.MinValue;
                Thread.Sleep(250);
            }
            if (splashLikeWindowSeen) throw new InvalidOperationException("splash_not_ready:ok_nte_formal_gui_never_became_interactive");
            throw new TimeoutException("telemetry_missing:ok_nte_formal_update_timeout");
        }

        private static void ThrowIfCancellationRequested(Func<bool> cancellationRequested)
        {
            if (cancellationRequested != null && cancellationRequested())
                throw new OperationCanceledException("cancelled_by_manager");
        }

        private static bool RunReadOnlyProbe(Func<bool> probe, TimeSpan timeout, Func<bool> cancellationRequested)
        {
            ThrowIfCancellationRequested(cancellationRequested);
            bool result = false;
            Exception failure = null;
            // A cross-process accessibility provider can stop returning. Keep
            // that read-only call off the protocol thread so deadline/cancel
            // checks still run. At most one abandoned probe exists: a timeout
            // ends this run, and this background thread cannot keep it alive.
            var worker = new Thread(delegate()
            {
                try { result = probe(); }
                catch (Exception error) { failure = error; }
            });
            worker.IsBackground = true;
            worker.SetApartmentState(ApartmentState.MTA);
            var elapsed = Stopwatch.StartNew();
            worker.Start();
            while (!worker.Join(50))
            {
                ThrowIfCancellationRequested(cancellationRequested);
                if (elapsed.Elapsed >= timeout)
                    throw new TimeoutException("telemetry_missing:ok_nte_formal_accessibility_timeout");
            }
            ThrowIfCancellationRequested(cancellationRequested);
            if (failure != null) throw failure;
            return result;
        }

        private static bool IsInteractiveFormalWindow(IntPtr window, Func<bool> cancellationRequested, out bool splashLike)
        {
            bool observedSplash = false;
            bool result = RunReadOnlyProbe(
                delegate { return ReadInteractiveFormalWindow(window, out observedSplash); },
                TimeSpan.FromSeconds(2), cancellationRequested);
            splashLike = observedSplash;
            return result;
        }

        private static bool ReadInteractiveFormalWindow(IntPtr window, out bool splashLike)
        {
            splashLike = false;
            try
            {
                AutomationElement root = AutomationElement.FromHandle(window);
                if (root == null) { splashLike = true; return false; }
                string identity = ((root.Current.Name ?? String.Empty) + " " +
                    (root.Current.AutomationId ?? String.Empty) + " " +
                    (root.Current.ClassName ?? String.Empty)).ToLowerInvariant();
                if (identity.Contains("splash") || identity.Contains("logo") || identity.Contains("loading") ||
                    identity.Contains("启动") || identity.Contains("加载"))
                { splashLike = true; return false; }
                Condition interactiveType = new OrCondition(
                    new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button),
                    new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit),
                    new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Tab),
                    new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Menu),
                    new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.List));
                AutomationElement element = root.FindFirst(TreeScope.Descendants, interactiveType);
                if (element != null && element.Current.IsEnabled && !element.Current.IsOffscreen) return true;
                splashLike = true;
                return false;
            }
            catch
            {
                splashLike = true;
                return false;
            }
        }

        private static void PatchFormalMain(string path, string original, string bridgePath)
        {
            if (!original.Contains("if __name__ == \"__main__\":") ||
                !original.Contains("from src.config import config") ||
                original.Contains("YEYU_GAMER_NTE_BRIDGE_V1"))
                throw new InvalidOperationException("ok_nte_main_identity_changed");
            string prefix =
                "# YEYU_GAMER_NTE_BRIDGE_V1\n" +
                "import os as _yeyu_os, sys as _yeyu_sys\n" +
                "if _yeyu_os.environ.get('YEYU_GAMER_STAGE_FILE'):\n" +
                "    _yeyu_sys.path.insert(0, _yeyu_os.environ['YEYU_GAMER_NTE_BRIDGE_ROOT'])\n" +
                "    from NteYeYuBridge import install as _yeyu_install\n" +
                "    _yeyu_install()\n" +
                "    _yeyu_sys.argv.extend(['--task', '2', '--exit'])\n";
            File.WriteAllText(path, prefix + original, new UTF8Encoding(false));
        }

        private static Process WaitForInner(ToolBinding binding, DateTimeOffset startedAt, TimeSpan timeout, Func<bool> cancellationRequested)
        {
            DateTimeOffset deadline = DateTimeOffset.UtcNow.Add(timeout);
            string expected = Path.GetFullPath(binding.Pythonw);
            while (DateTimeOffset.UtcNow < deadline)
            {
                if (cancellationRequested != null && cancellationRequested()) throw new OperationCanceledException("cancelled_by_manager");
                foreach (Process process in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(binding.Pythonw)))
                {
                    bool keep = false;
                    try
                    {
                        keep = !process.HasExited && process.StartTime.ToUniversalTime() >= startedAt.UtcDateTime.AddSeconds(-2) &&
                            String.Equals(Path.GetFullPath(process.MainModule.FileName), expected, StringComparison.OrdinalIgnoreCase);
                    }
                    catch { }
                    if (keep) return process;
                    process.Dispose();
                }
                Thread.Sleep(250);
            }
            throw new TimeoutException("ok_nte_formal_gui_handoff_timeout");
        }

        private static List<Process> FindFormalProcesses(ToolBinding binding, DateTimeOffset startedAt)
        {
            var result = new List<Process>();
            string expected = Path.GetFullPath(binding.FormalLauncher);
            foreach (Process process in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(binding.FormalLauncher)))
            {
                bool keep = false;
                try
                {
                    bool timeMatches = startedAt == DateTimeOffset.MinValue ||
                        process.StartTime.ToUniversalTime() >= startedAt.UtcDateTime.AddSeconds(-2);
                    keep = !process.HasExited && timeMatches &&
                        String.Equals(Path.GetFullPath(process.MainModule.FileName), expected, StringComparison.OrdinalIgnoreCase);
                }
                catch { }
                if (keep) result.Add(process); else process.Dispose();
            }
            return result;
        }

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);

        private static void WaitForFormalExit(Func<bool> anyRunning, Func<bool> cancellationRequested)
        {
            var elapsed = Stopwatch.StartNew();
            TimeSpan? cancelObservedAt = null;
            while (anyRunning())
            {
                if (cancellationRequested != null && cancellationRequested() && !cancelObservedAt.HasValue)
                    cancelObservedAt = elapsed.Elapsed;
                // Host allows five seconds for the terminal event. Preserve a
                // short graceful-close interval, then stop only the bound
                // run-owned helper; never wait ten seconds after cancellation.
                if (cancelObservedAt.HasValue && elapsed.Elapsed - cancelObservedAt.Value >= TimeSpan.FromMilliseconds(500)) return;
                if (elapsed.Elapsed >= TimeSpan.FromSeconds(10)) return;
                Thread.Sleep(50);
            }
        }

        private static void StopFormalProcesses(ToolBinding binding, DateTimeOffset startedAt, Func<bool> cancellationRequested, Process inner = null)
        {
            List<Process> processes = FindFormalProcesses(binding, startedAt);
            // WaitForInner already checked this exact process against the
            // bound managed Python path and this run's start time. Disposing
            // its handle alone would leave daily automation alive on cancel.
            if (inner != null) processes.Add(inner);
            foreach (Process process in processes)
            {
                // Posting WM_CLOSE cannot wait for an unresponsive GUI handler.
                try { if (!process.HasExited) PostMessage(process.MainWindowHandle, 0x0010, IntPtr.Zero, IntPtr.Zero); } catch { }
            }
            WaitForFormalExit(
                delegate { return processes.Any(process => { try { return !process.HasExited; } catch { return false; } }); },
                cancellationRequested);
            foreach (Process process in processes)
            {
                try { if (!process.HasExited) process.Kill(); } catch { }
                finally { process.Dispose(); }
            }
        }

        private static void ReadFormalMode(ToolBinding binding, out bool autoStart, out bool admin)
        {
            IDictionary<string, object> document = Json.DeserializeObject(File.ReadAllText(binding.UpdateState, Encoding.UTF8)) as IDictionary<string, object>;
            if (document == null || !(document["auto_start"] is bool) || !(document["profiles"] is object[]) || !(document["current_profile"] is string))
                throw new InvalidOperationException("ok_nte_update_state_invalid");
            autoStart = (bool)document["auto_start"];
            IDictionary<string, object> profile = ((object[])document["profiles"]).Cast<object>()
                .Select(item => item as IDictionary<string, object>)
                .SingleOrDefault(item => item != null && item["name"] as string == document["current_profile"] as string);
            if (profile == null || !(profile["admin"] is bool)) throw new InvalidOperationException("ok_nte_profile_invalid");
            admin = (bool)profile["admin"];
        }

        private static void SetFormalMode(ToolBinding binding, bool autoStart, bool admin)
        {
            IDictionary<string, object> document = Json.DeserializeObject(File.ReadAllText(binding.UpdateState, Encoding.UTF8)) as IDictionary<string, object>;
            object[] profiles = document == null ? null : document["profiles"] as object[];
            string current = document == null ? null : document["current_profile"] as string;
            IDictionary<string, object> profile = profiles == null ? null : profiles.Cast<object>()
                .Select(item => item as IDictionary<string, object>)
                .SingleOrDefault(item => item != null && item["name"] as string == current);
            if (document == null || profile == null || !(document["auto_start"] is bool) || !(profile["admin"] is bool))
                throw new InvalidOperationException("ok_nte_update_state_invalid");
            document["auto_start"] = autoStart;
            profile["admin"] = admin;
            string temporary = binding.UpdateState + ".yeyu.tmp";
            File.WriteAllText(temporary, Json.Serialize(document), new UTF8Encoding(false));
            File.Replace(temporary, binding.UpdateState, null);
        }

        private static string ReadFormalVersion(ToolBinding binding)
        {
            try
            {
                IDictionary<string, object> state = Json.DeserializeObject(File.ReadAllText(binding.UpdateState, Encoding.UTF8)) as IDictionary<string, object>;
                return state == null ? "unknown" : state["current_version"] as string ?? "unknown";
            }
            catch { return "unknown"; }
        }

        private static void DrainStageEvents(string path, HashSet<string> seen, Action<string, string, string> onStage)
        {
            string[] lines;
            try { lines = ReadSharedLines(path); }
            catch (IOException) { return; }
            foreach (string line in lines)
            {
                if (String.IsNullOrWhiteSpace(line) || !seen.Add(line)) continue;
                IDictionary<string, object> record;
                try { record = Json.DeserializeObject(line) as IDictionary<string, object>; }
                catch { continue; }
                if (record == null) continue;
                string operation = record.ContainsKey("operation") ? record["operation"] as string : null;
                string state = record.ContainsKey("state") ? record["state"] as string : null;
                string stageDetail = record.ContainsKey("detail") ? record["detail"] as string : String.Empty;
                if (!String.IsNullOrWhiteSpace(operation) && !String.IsNullOrWhiteSpace(state))
                    onStage(operation, state, stageDetail ?? String.Empty);
            }
        }

        private static string[] ReadSharedLines(string path)
        {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            using (var reader = new StreamReader(stream, Encoding.UTF8, true))
                return reader.ReadToEnd().Split(new string[] { "\r\n", "\n" }, StringSplitOptions.RemoveEmptyEntries);
        }

        private static string LocalDirectory(string path, string error)
        {
            if (String.IsNullOrWhiteSpace(path) || !Path.IsPathRooted(path)) throw new InvalidOperationException(error);
            string full = Path.GetFullPath(path).TrimEnd('\\');
            if (full.StartsWith("\\\\", StringComparison.Ordinal) || !Directory.Exists(full) ||
                (File.GetAttributes(full) & FileAttributes.ReparsePoint) != 0) throw new InvalidOperationException(error);
            return full;
        }

        private static string LocalFile(string path, string expectedHash, string error)
        {
            if (String.IsNullOrWhiteSpace(path) || !Path.IsPathRooted(path)) throw new InvalidOperationException(error);
            string full = Path.GetFullPath(path);
            if (full.StartsWith("\\\\", StringComparison.Ordinal) || !File.Exists(full) ||
                (File.GetAttributes(full) & FileAttributes.ReparsePoint) != 0) throw new InvalidOperationException(error);
            if (!String.IsNullOrWhiteSpace(expectedHash) && Hash(full) != expectedHash) throw new InvalidOperationException(error);
            return full;
        }

        private static int ProbeBinding()
        {
            try
            {
                ToolBinding binding = LoadBinding(false);
                Console.Out.WriteLine(Json.Serialize(new Dictionary<string, object>
                {
                    { "ok", true }, { "gameId", "NTE" }, { "processStarted", false },
                    { "dailyTaskSha256", Hash(binding.DailyTask) }, { "guiEntrySha256", Hash(binding.GuiEntry) }
                }));
                return 0;
            }
            catch (Exception error)
            {
                Console.Out.WriteLine(Json.Serialize(new Dictionary<string, object>
                {
                    { "ok", false }, { "gameId", "NTE" }, { "processStarted", false },
                    { "code", error.Message }
                }));
                return 65;
            }
        }

        private static Dictionary<string, object> Base(IDictionary<string, object> request, string type, int sequence,
            IDictionary<string, object> extra)
        {
            var value = new Dictionary<string, object>
            {
                { "schemaVersion", 1 }, { "protocolVersion", ProtocolVersion }, { "eventType", type },
                { "sequence", sequence }, { "runId", request["runId"] },
                { "runAttemptId", request["runAttemptId"] }, { "fencingToken", request["fencingToken"] },
                { "gameId", "NTE" }, { "at", DateTime.UtcNow.ToString("o") }
            };
            foreach (KeyValuePair<string, object> item in extra) value[item.Key] = item.Value;
            return value;
        }

        private static int TerminalSetupFailure(IDictionary<string, object> request, object[] ids, string reason)
        {
            string[] unresolved = ids.Cast<string>().ToArray();
            Emit(Base(request, "hello", 0, new Dictionary<string, object>
            {
                { "packageId", PackageId }, { "packageVersion", PackageVersion() },
                { "packageDigest", PackageDigest() }, { "runnerPid", Process.GetCurrentProcess().Id },
                { "acceptedTodoInstanceIds", ids }
            }));
            Emit(Base(request, "run_terminal", 1, new Dictionary<string, object>
            {
                { "status", "review_required" }, { "transportOutcome", "clean" },
                { "attemptedTodoInstanceIds", new string[0] }, { "completedTodoInstanceIds", new string[0] },
                { "unresolvedTodoInstanceIds", ids },
                { "terminalEventDigest", TerminalDigest(request, "review_required", new string[0], new string[0], unresolved, 0) },
                { "exitCode", 0 }
            }));
            return 0;
        }

        private static void Emit(IDictionary<string, object> value)
        {
            Console.Out.WriteLine(Json.Serialize(value));
            Console.Out.Flush();
        }

        private static bool NumberEquals(object value, int expected)
        {
            return (value is int && (int)value == expected) || (value is decimal && (decimal)value == expected);
        }

        private static int AttemptNumber(IDictionary<string, object> todo)
        {
            object value = todo == null || !todo.ContainsKey("priorAttempts") ? null : todo["priorAttempts"];
            if (value is int && (int)value >= 0) return checked((int)value + 1);
            if (value is decimal && decimal.Truncate((decimal)value) == (decimal)value && (decimal)value >= 0 && (decimal)value < Int32.MaxValue) return checked((int)(decimal)value + 1);
            throw new InvalidOperationException("prior_attempts_invalid");
        }

        private static string SafeDetail(string value)
        {
            string result = value ?? String.Empty;
            return result.Length <= 1000 ? result : result.Substring(0, 1000);
        }

        private static string TypedFailureReason(string detail, string fallback)
        {
            foreach (string code in new[] { "client_update_required", "launcher_download_required", "splash_not_ready", "telemetry_missing" })
                if (!String.IsNullOrEmpty(detail) && detail.StartsWith(code, StringComparison.Ordinal)) return code;
            return fallback;
        }

        private static string TypedStartupFailure(Exception error)
        {
            string message = SafeDetail(error == null ? String.Empty : error.Message);
            string code = TypedFailureReason(message, "tool_start_failed");
            if (!String.IsNullOrEmpty(message) &&
                message.StartsWith(code + ":", StringComparison.Ordinal)) return message;
            return code + ":" + (String.IsNullOrEmpty(message) ? (error == null ? "unknown" : error.GetType().Name) : message);
        }

        private static string Hash(string path)
        {
            using (FileStream stream = File.OpenRead(path))
            using (SHA256 sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", String.Empty).ToLowerInvariant();
        }

        private static string PackageVersion()
        {
            try
            {
                IDictionary<string, object> manifest = Json.DeserializeObject(
                    File.ReadAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "install-manifest.json"), Encoding.UTF8))
                    as IDictionary<string, object>;
                return manifest["packageVersion"] as string;
            }
            catch { return "unknown"; }
        }

        private static string PackageDigest()
        {
            try
            {
                string root = AppDomain.CurrentDomain.BaseDirectory;
                byte[] raw = File.ReadAllBytes(Path.Combine(root, "install-manifest.json"));
                IDictionary<string, object> document = Json.DeserializeObject(Encoding.UTF8.GetString(raw)) as IDictionary<string, object>;
                object[] files = document["files"] as object[];
                var entries = new SortedDictionary<string, string>(StringComparer.Ordinal);
                foreach (object item in files)
                {
                    IDictionary<string, object> file = item as IDictionary<string, object>;
                    entries.Add(file["path"] as string, file["sha256"] as string);
                }
                using (SHA256 sha = SHA256.Create())
                using (var stream = new MemoryStream())
                {
                    stream.Write(raw, 0, raw.Length);
                    foreach (KeyValuePair<string, string> item in entries)
                    {
                        byte[] name = Encoding.UTF8.GetBytes(item.Key);
                        byte[] digest = Encoding.ASCII.GetBytes(item.Value);
                        stream.Write(name, 0, name.Length);
                        stream.Write(digest, 0, digest.Length);
                    }
                    return "sha256:" + BitConverter.ToString(sha.ComputeHash(stream.ToArray())).Replace("-", String.Empty).ToLowerInvariant();
                }
            }
            catch (Exception error) { throw new InvalidDataException("package_digest_unavailable", error); }
        }

        private static string TerminalDigest(IDictionary<string, object> request, string status, string[] attempted,
            string[] completed, string[] unresolved, int exitCode)
        {
            string payload = (request["runId"] as string) + "\n" + (request["runAttemptId"] as string) + "\n" + status +
                "\n" + String.Join("\n", attempted) + "\n--completed--\n" + String.Join("\n", completed) +
                "\n--unresolved--\n" + String.Join("\n", unresolved) + "\n" + exitCode;
            using (SHA256 sha = SHA256.Create())
                return "sha256:" + BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(payload))).Replace("-", String.Empty).ToLowerInvariant();
        }
    }
}
