using System;
using System.Reflection;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;

namespace YeYuGamer.LocalDailyAdapter
{
    // This package accepts only three fixed local daily-tool bindings. Neither
    // the Manager request nor the Web UI can supply a command, arguments, or path.
    internal static class Program
    {
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
        private static readonly TextReader ProtocolInput = new StreamReader(
            Console.OpenStandardInput(), new UTF8Encoding(false), false);
        private static readonly TextWriter ProtocolOutput = new StreamWriter(
            Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true };
        private const string PackageId = "legacy-night-rain-gamer";
        private const string ProtocolVersion = "1.1";

        private sealed class Binding
        {
            public string GameId;
            public string Game;
            public string ToolRoot;
            public string WorkingDirectory;
            public string Executable;
            public string Arguments;
            public string DailyConfig;
            public IDictionary<string, object> DailyTaskProfile;
            public bool Gf2SupplyRewardChoiceSupported;
            public bool Gf2RewardInspectionSupported;
            public bool Gf2RewardInspectionOnly;
            public string UpdateState;
            public string InnerPython;
            public string InnerEntry;
            public string AppName;
            public bool WwInspectionSupported;
            public bool WwInspectionOnly;
            public bool WwRewardInspectionSupported;
            public bool WwRewardInspectionOnly;
            public bool WwBattlePassInspectionSupported;
            public bool WwBattlePassInspectionOnly;
            public HashSet<string> WwInspectionOperations;
            public bool EndfieldRewardInspectionSupported;
            public bool EndfieldRewardInspectionOnly;
            public string AccountId = "default";
            public IDictionary<string, object> AccountSnapshot;
            public string AccountOperation;
            public bool AccountEnabled { get { return AccountSnapshot != null && AccountSnapshot.Count != 0; } }
            public string SavedAccountLabel {
                get {
                    if (AccountSnapshot == null) return String.Empty;
                    return AccountSnapshot.ContainsKey("saved_account_label") ? AccountSnapshot["saved_account_label"] as string : String.Empty;
                }
            }
        }

        private sealed class ConfigSnapshot {
            public string Path;
            public byte[] Content;
            public bool Existed;
        }

        private static bool IsEndfieldSplitConfig(Binding binding) {
            return binding.GameId == "Endfield" && !String.IsNullOrEmpty(binding.WorkingDirectory) &&
                File.Exists(Path.Combine(binding.WorkingDirectory, "src", "tasks", "daily", "daily_feature.py"));
        }

        private static readonly string[] EndfieldTaskGroupKeys = {
            "帝江号任务", "其他任务", "战斗任务", "奖励任务", "自动送货任务", "收尾任务"
        };

        private static void ApplyEndfieldTaskGroups(Binding binding, HashSet<string> selected, IDictionary<string, object> config) {
            string source = Path.Combine(binding.WorkingDirectory ?? String.Empty, "src", "tasks", "onetime", "DailyTask.py");
            if (!File.Exists(source)) return;
            string schema = File.ReadAllText(source, Encoding.UTF8);
            if (!schema.Contains("CFG_BOAT_TASKS = \"帝江号任务\"") ||
                !schema.Contains("CFG_REWARD_TASKS = \"奖励任务\"")) return;
            // Official v1.1.9 predicates read these persisted lists before
            // legacy bool switches. Project only the frozen operation scope;
            // all execution order and subtask parameters remain official.
            config["帝江号任务"] = selected.Contains("dijiang-harvest") ? new[] { "帝江号收菜" } : new string[0];
            var other = new List<string>();
            if (selected.Contains("mail")) other.Add("收邮件");
            if (selected.Contains("delivery-commission")) other.Add("转交运送委托");
            if (selected.Contains("collect-credit")) other.Add("收信用");
            config["其他任务"] = other;
            config["战斗任务"] = selected.Contains("spend-sanity") ? new[] { "刷体力" } : new string[0];
            config["奖励任务"] = selected.Contains("claim-daily-reward") ? new[] { "日常奖励" } : new string[0];
            config["自动送货任务"] = new string[0];
            config["收尾任务"] = new string[0];
        }

        private static List<ConfigSnapshot> SnapshotEndfieldSplitConfig(Binding binding) {
            var snapshots = new List<ConfigSnapshot>();
            if (!IsEndfieldSplitConfig(binding)) return snapshots;
            string directory = Path.GetDirectoryName(binding.DailyConfig);
            string marker = Path.Combine(directory, "_daily_split_migrations.json");
            var state = File.Exists(marker) ? Json.DeserializeObject(File.ReadAllText(marker, Encoding.UTF8)) as IDictionary<string, object> : null;
            object raw;
            var completed = state != null && state.TryGetValue("completed_batches", out raw) ? raw as IEnumerable : null;
            var batches = new HashSet<string>(completed == null ? new string[0] : completed.Cast<object>().OfType<string>(), StringComparer.Ordinal);
            if (!new[] { "daily_split_pilot_v1", "daily_split_v2", "daily_split_v3" }.All(batches.Contains))
                throw new InvalidOperationException("endfield_official_config_migration_required");
            // The official migration must finish in the isolated candidate
            // before temporary scope values can be imported as user settings.
            foreach (string name in new[] { "DailyBattleTask", "BoatOrganizeTask", "BoatHarvestTask", "RegionalBuildTask", "ActivityRewardTask", "account_scoped_overrides" }) {
                string path = Path.Combine(directory, name + ".json");
                RestorePendingDailyConfig(new Binding { Executable = binding.Executable, DailyConfig = path });
                bool existed = File.Exists(path);
                snapshots.Add(new ConfigSnapshot { Path = path, Existed = existed, Content = existed ? File.ReadAllBytes(path) : null });
            }
            // Make recovery durable before the first projected write. A tool
            // crash or adapter termination cannot discard the original bytes.
            foreach (ConfigSnapshot snapshot in snapshots)
                PreserveDailyConfigForRecovery(snapshot.Path, snapshot.Content, snapshot.Existed);
            return snapshots;
        }

        private static void RestoreEndfieldSplitConfig(List<ConfigSnapshot> snapshots, bool stopped) {
            var failures = new List<Exception>();
            foreach (ConfigSnapshot snapshot in snapshots) {
                try {
                    if (stopped) {
                        RestoreOrPreserveDailyConfig(snapshot.Path, snapshot.Content, snapshot.Existed);
                        File.Delete(snapshot.Path + ".yeyu-restore-pending");
                    } else PreserveDailyConfigForRecovery(snapshot.Path, snapshot.Content, snapshot.Existed);
                } catch (Exception error) { failures.Add(error); }
            }
            if (failures.Count != 0) throw new AggregateException("endfield_split_config_restore_failed", failures);
        }

        private static Dictionary<string, IDictionary<string, object>> EndfieldSplitOverrides(HashSet<string> selected) {
            var maps = new Dictionary<string, IDictionary<string, object>>(StringComparer.Ordinal) {
                { "BoatOrganizeTask", new Dictionary<string, object> { { "⭐帝江号一键存放", false }, { "⭐简易制作", false } } },
                { "RegionalBuildTask", new Dictionary<string, object> { { "⭐地区建设", new string[0] } } },
                { "ActivityRewardTask", new Dictionary<string, object> { { "⭐活动奖励", new string[0] } } },
                { "BoatHarvestTask", new Dictionary<string, object>() }
            };
            if (!selected.Contains("dijiang-harvest")) maps["BoatHarvestTask"]["⭐帝江号收菜"] = new string[0];
            return maps;
        }

        private static void WriteConfigDocument(string path, IDictionary<string, object> config) {
            string temporary = path + ".yeyu." + Guid.NewGuid().ToString("N") + ".tmp";
            try {
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                File.WriteAllText(temporary, Json.Serialize(config), new UTF8Encoding(false));
                if (File.Exists(path)) ReplaceConfigFile(temporary, path);
                else File.Move(temporary, path);
            } finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }

        private static void ReplaceConfigFile(string temporary, string path) {
            // A short Windows file lock must not break scope projection. Keep
            // replacement atomic; never delete/truncate the user's destination.
            for (int attempt = 0; ; attempt++) {
                try { File.Replace(temporary, path, null); return; }
                catch (IOException error) {
                    int code = error.HResult & 0xffff;
                    if (attempt >= 5 || (code != 32 && code != 33 && code != 1175) ||
                        !File.Exists(temporary) || !File.Exists(path)) throw;
                    Thread.Sleep(100);
                }
            }
        }

        private static void ApplyEndfieldSplitConfig(Binding binding, HashSet<string> selected, IDictionary<string, object> daily) {
            if (!IsEndfieldSplitConfig(binding)) return;
            string directory = Path.GetDirectoryName(binding.DailyConfig);
            var maps = EndfieldSplitOverrides(selected);
            foreach (var entry in maps) {
                if (entry.Value.Count == 0) continue;
                string path = Path.Combine(directory, entry.Key + ".json");
                var config = File.Exists(path) ? Json.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as IDictionary<string, object> : new Dictionary<string, object>();
                if (config == null) throw new InvalidOperationException("endfield_split_config_invalid");
                foreach (var item in entry.Value) config[item.Key] = item.Value;
                WriteConfigDocument(path, config);
            }
            string accountPath = Path.Combine(directory, "account_scoped_overrides.json");
            if (!File.Exists(accountPath)) return;
            var store = Json.DeserializeObject(File.ReadAllText(accountPath, Encoding.UTF8)) as IDictionary<string, object>;
            object raw;
            var accounts = store != null && store.TryGetValue("accounts", out raw) ? raw as IDictionary<string, object> : null;
            if (accounts == null) throw new InvalidOperationException("endfield_account_scope_invalid");
            maps["DailyTask"] = daily.Where(item => (item.Key.StartsWith("⭐", StringComparison.Ordinal) || EndfieldTaskGroupKeys.Contains(item.Key)) &&
                (item.Key != "⭐帝江号收菜" || !selected.Contains("dijiang-harvest"))).ToDictionary(item => item.Key, item => item.Value);
            foreach (object value in accounts.Values) {
                var tasks = value as IDictionary<string, object>;
                if (tasks == null) throw new InvalidOperationException("endfield_account_scope_invalid");
                foreach (var entry in maps) {
                    if (entry.Value.Count == 0) continue;
                    object segment;
                    var overrides = tasks.TryGetValue(entry.Key, out segment) ? segment as IDictionary<string, object> : new Dictionary<string, object>();
                    if (overrides == null) throw new InvalidOperationException("endfield_account_scope_invalid");
                    foreach (var item in entry.Value) overrides[item.Key] = item.Value;
                    tasks[entry.Key] = overrides;
                }
            }
            WriteConfigDocument(accountPath, store);
        }

        public static int Main(string[] args)
        {
            // The Adapter protocol is UTF-8 JSONL.  Explicitly pin redirected
            // console streams so localized stage details never fall back to
            // the Windows OEM code page and corrupt a single event frame.
            if (args != null && args.Length == 1 && args[0] == "--probe-binding")
            {
                bool ok = Probe();
                ProtocolOutput.WriteLine(Json.Serialize(new Dictionary<string, object> {
                    { "ok", ok }, { "processStarted", false },
                    { "gameIds", new [] { "WW", "Endfield", "GF2" } }
                }));
                ProtocolOutput.Flush();
                return ok ? 0 : 65;
            }
            Dictionary<string, string> options = ParseArguments(args);
            if (options == null || options["--protocol-version"] != ProtocolVersion || !SupportedGame(options["--game-id"])) return 64;
            IDictionary<string, object> request = Json.DeserializeObject(ProtocolInput.ReadLine() ?? String.Empty) as IDictionary<string, object>;
            if (!MatchesRequest(request, options)) return 64;
            object[] ids = request["executableTodoInstanceIds"] as object[];
            object[] todos = request["todos"] as object[];
            if (ids == null || todos == null || ids.Length == 0 || ids.Length != todos.Length) return 64;
            Binding binding;
            try { binding = LoadBinding(options["--game-id"]); }
            catch (Exception error) { return TerminalSetupFailure(request, ids, error.Message); }
            string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            if (String.IsNullOrEmpty(staging)) return 65;
            Directory.CreateDirectory(staging);
            Emit(Base(request, binding.GameId, "hello", 0, new Dictionary<string, object> {
                { "packageId", PackageId }, { "packageVersion", PackageVersion() }, { "packageDigest", PackageDigest() },
                { "runnerPid", Process.GetCurrentProcess().Id }, { "acceptedTodoInstanceIds", ids }
            }));
            int sequence = 1;
            var attempted = new List<string>();
            var attempts = new Dictionary<string, string>(StringComparer.Ordinal);
            var todosByOperation = new Dictionary<string, Tuple<string, IDictionary<string, object>>>(StringComparer.Ordinal);
            var terminal = new HashSet<string>(StringComparer.Ordinal);
            var completed = new HashSet<string>(StringComparer.Ordinal);
            var failedTodos = new HashSet<string>(StringComparer.Ordinal);
            bool upstreamDailyFailed = false;
            for (int index = 0; index < ids.Length; index++) {
                string id = ids[index] as string;
                IDictionary<string, object> todo = todos[index] as IDictionary<string, object>;
                if (String.IsNullOrEmpty(id) || todo == null || !AllowedOperation(binding.GameId, todo["operation"] as string)) return 64;
                string attemptId = Guid.NewGuid().ToString();
                string operation = todo["operation"] as string;
                if (String.IsNullOrEmpty(operation) || todosByOperation.ContainsKey(operation)) return 64;
                attempts.Add(id, attemptId);
                todosByOperation.Add(operation, Tuple.Create(id, todo));
            }
            ConfigureAccountBinding(binding, request, todos);
            var cancellationRequested = new ManualResetEvent(false);
            var controlReader = new Thread(() => {
                try {
                    string line;
                    while ((line = ProtocolInput.ReadLine()) != null) {
                        IDictionary<string, object> control = Json.DeserializeObject(line) as IDictionary<string, object>;
                        if (MatchesCancel(control, request)) {
                            cancellationRequested.Set();
                            return;
                        }
                    }
                } catch (Exception) { }
            });
            controlReader.IsBackground = true;
            controlReader.Start();
            Action<string> startTodo = operation => {
                Tuple<string, IDictionary<string, object>> target;
                if (!todosByOperation.TryGetValue(operation, out target) || attempted.Contains(target.Item1)) return;
                attempted.Add(target.Item1);
                Emit(Base(request, binding.GameId, "todo_attempt_started", sequence++, new Dictionary<string, object> {
                    { "todoInstanceId", target.Item1 }, { "todoAttemptId", attempts[target.Item1] }, { "attemptNo", AttemptNumber(target.Item2) }, { "operation", operation },
                    { "stepCaptureSource", "adapter" }
                }));
            };
            var wwSidecarStepEvidence = new Dictionary<string, List<Tuple<string, string, string>>>(StringComparer.Ordinal);
            var pendingTerminals = new List<Tuple<string, string, string>>();
            var pendingTerminalOperations = new HashSet<string>(StringComparer.Ordinal);
            bool toolFinished = false;
            bool humanChoiceRequired = false;
            var stagedUpstreamLogs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var pendingUntaggedStages = new List<Tuple<string, string, string>>();
            string officialCurrentAccount = String.Empty;
            bool wwUnattributedStartupPublished = false;
            string observationFile = Environment.GetEnvironmentVariable("YEYU_GAMER_WW_MULTI_OBSERVATION_FILE");
            string observationMode = Environment.GetEnvironmentVariable("YEYU_GAMER_WW_MULTI_OBSERVATION_MODE");
            binding.WwInspectionOnly = binding.GameId == "WW" && WwShouldUseInspection(
                todosByOperation.Keys, RequestedSelection(request), binding.WwInspectionSupported);
            binding.WwRewardInspectionOnly = binding.GameId == "WW" && WwShouldUseRewardInspection(
                todosByOperation.Keys, binding.WwRewardInspectionSupported);
            binding.WwBattlePassInspectionOnly = binding.GameId == "WW" && WwShouldUseBattlePassInspection(
                todosByOperation.Keys, binding.WwBattlePassInspectionSupported);
            var wwInspectionGroups = WwInspectionGroups(binding, todosByOperation.Keys.ToArray());
            binding.WwInspectionOperations = new HashSet<string>(
                wwInspectionGroups.SelectMany(group => group).Where(operation => operation != "attach-world"),
                StringComparer.Ordinal);
            binding.EndfieldRewardInspectionOnly = binding.GameId == "Endfield" && EndfieldShouldUseRewardInspection(
                binding.GameId, binding.AccountId, todosByOperation.Keys, request, binding.EndfieldRewardInspectionSupported);
            if (binding.EndfieldRewardInspectionOnly) {
                binding.Arguments = "\"" + binding.InnerEntry + "\" --task 32 --exit";
                binding.DailyConfig = Path.Combine(binding.WorkingDirectory, "configs", "RewardStatusInspectionTask.json");
            }
            binding.Gf2RewardInspectionOnly = binding.GameId == "GF2" && Gf2ShouldUseRewardInspection(
                binding.AccountId, todosByOperation.Keys, request, binding.Gf2RewardInspectionSupported);
            if (binding.Gf2RewardInspectionOnly) {
                binding.Arguments = "\"" + binding.InnerEntry + "\" --task 7 --exit";
                binding.DailyConfig = Path.Combine(binding.WorkingDirectory, "configs", "RewardStatusInspectionTask.json");
            }
            if (binding.WwInspectionOnly || binding.WwRewardInspectionOnly || binding.WwBattlePassInspectionOnly || wwInspectionGroups.Count > 1) observationMode = String.Empty;
            bool wwObservationWrite = binding.GameId == "WW" && String.Equals(observationMode, "write", StringComparison.Ordinal);
            bool wwObservationRead = binding.GameId == "WW" && String.Equals(observationMode, "read", StringComparison.Ordinal);
            bool wwLiveToolStarted = false;
            Action<string, string, string> emitTerminalTodo = (operation, state, stageDetail) => {
                Tuple<string, IDictionary<string, object>> target;
                if (!todosByOperation.TryGetValue(operation, out target) || terminal.Contains(target.Item1)) return;
                // A launcher/update failure is not an attempt of every daily
                // operation.  Only an upstream `started` event may open a Todo
                // attempt; otherwise the Todo stays unresolved and unattempted.
                if (!attempted.Contains(target.Item1)) return;
                string safeStageDetail = String.IsNullOrWhiteSpace(stageDetail)
                    ? binding.GameId + " upstream stage " + state
                    : stageDetail;
                string id = target.Item1;
                string attemptId = attempts[id];
                var evidenceArtifactIds = new List<string>();
                List<Tuple<string, string, string>> sidecarEvidence;
                if (binding.GameId == "WW" && wwSidecarStepEvidence.TryGetValue(operation, out sidecarEvidence)) {
                    foreach (Tuple<string, string, string> artifact in sidecarEvidence) {
                        string imagePath = Path.Combine(staging, artifact.Item1);
                        if (!File.Exists(imagePath)) continue;
                        if (artifact.Item2 == "game-ui-key-step-verification-raw")
                            WriteWwInspectionScope(binding, request, id, attemptId, imagePath, artifact.Item3);
                        if (artifact.Item2 == "game-ui-reward-verification-raw")
                            WriteWwVerificationScope(binding, request, id, attemptId, imagePath, artifact.Item3, operation);
                        string imageArtifactId = Guid.NewGuid().ToString();
                        Emit(Base(request, binding.GameId, "artifact_staged", sequence++, new Dictionary<string, object> {
                            { "todoInstanceId", id }, { "todoAttemptId", attemptId }, { "artifactId", imageArtifactId },
                            { "kind", artifact.Item2 }, { "fileName", artifact.Item1 }, { "mimeType", StepArtifactMimeType(artifact.Item2) },
                            { "sizeBytes", new FileInfo(imagePath).Length }, { "sha256", Hash(imagePath) }, { "capturedAt", artifact.Item3 }
                        }));
                        evidenceArtifactIds.Add(imageArtifactId);
                    }
                }
                string name = binding.GameId.ToLowerInvariant() + "-daily-" + Guid.NewGuid().ToString("N") + ".txt";
                string path = Path.Combine(staging, name);
                string launchTimeoutArtifact = DetailValue(safeStageDetail, "launchTimeoutArtifact");
                if (state == "failed" && !String.IsNullOrWhiteSpace(launchTimeoutArtifact) &&
                    String.Equals(Path.GetFileName(launchTimeoutArtifact), launchTimeoutArtifact, StringComparison.Ordinal) &&
                    File.Exists(Path.Combine(staging, launchTimeoutArtifact))) {
                    string imagePath = Path.Combine(staging, launchTimeoutArtifact);
                    string imageArtifactId = Guid.NewGuid().ToString();
                    Emit(Base(request, binding.GameId, "artifact_staged", sequence++, new Dictionary<string, object> {
                        { "todoInstanceId", id }, { "todoAttemptId", attemptId }, { "artifactId", imageArtifactId },
                        { "kind", "game-ui-launch-timeout" }, { "fileName", launchTimeoutArtifact }, { "mimeType", "image/png" },
                        { "sizeBytes", new FileInfo(imagePath).Length }, { "sha256", Hash(imagePath) },
                        { "capturedAt", String.IsNullOrWhiteSpace(DetailValue(safeStageDetail, "launchTimeoutCapturedAt")) ? DateTime.UtcNow.ToString("o") : DetailValue(safeStageDetail, "launchTimeoutCapturedAt") }
                    }));
                    evidenceArtifactIds.Add(imageArtifactId);
                }
                File.WriteAllText(path, "operation=" + operation + "; stageState=" + state + "; " + safeStageDetail, new UTF8Encoding(false));
                string artifactId = Guid.NewGuid().ToString();
                Emit(Base(request, binding.GameId, "artifact_staged", sequence++, new Dictionary<string, object> {
                    { "todoInstanceId", id }, { "todoAttemptId", attemptId }, { "artifactId", artifactId }, { "kind", "tool-log-outcome" },
                    { "fileName", name }, { "mimeType", "text/plain" }, { "sizeBytes", new FileInfo(path).Length }, { "sha256", Hash(path) }, { "capturedAt", DateTime.UtcNow.ToString("o") }
                }));
                evidenceArtifactIds.Add(artifactId);
                // Normal terminals are flushed after process exit, so one
                // immutable snapshot includes the final upstream log tail.
                // A human-required stop retains the log available at the stop.
                string[] upstreamLogs = Directory.GetFiles(staging, binding.GameId.ToLowerInvariant() + "-upstream-*.jsonl");
                foreach (string upstreamLog in upstreamLogs.Where(value => !Path.GetFileName(value).Contains("-snapshot-") && !stagedUpstreamLogs.Contains(value))) {
                    FileInfo upstreamInfo = new FileInfo(upstreamLog);
                    if (upstreamInfo.Length != 0) {
                        string snapshotName = binding.GameId.ToLowerInvariant() + "-upstream-snapshot-" + Guid.NewGuid().ToString("N") + ".jsonl";
                        string snapshotPath = Path.Combine(staging, snapshotName);
                        File.Copy(upstreamLog, snapshotPath, true);
                        FileInfo snapshotInfo = new FileInfo(snapshotPath);
                        stagedUpstreamLogs.Add(upstreamLog);
                        string upstreamArtifactId = Guid.NewGuid().ToString();
                        Emit(Base(request, binding.GameId, "artifact_staged", sequence++, new Dictionary<string, object> {
                            { "todoInstanceId", id }, { "todoAttemptId", attemptId }, { "artifactId", upstreamArtifactId },
                            { "kind", "tool-log-upstream" }, { "fileName", snapshotName }, { "mimeType", "text/plain" },
                            { "sizeBytes", snapshotInfo.Length }, { "sha256", Hash(snapshotPath) }, { "capturedAt", upstreamInfo.LastWriteTimeUtc.ToString("o") }
                        }));
                        evidenceArtifactIds.Add(upstreamArtifactId);
                    }
                }
                bool failed = state == "failed";
                bool skipped = state == "skipped";
                bool completedStage = state == "completed";
                bool humanRequired = state == "human_required";
                bool unsupported = state == "unsupported";
                bool uncertain = state == "uncertain";
                // A clean process exit is transport evidence only. Every
                // supported upstream tool must emit a structured stage event
                // before a selected Todo can be considered completed.
                // "not needed today" is a successful terminal outcome for a
                // selected conditional daily.  Preserve the upstream reason
                // and artifact, but do not leave the current-day obligation
                // permanently skipped/deferred.
                string terminalStatus = humanRequired ? "human_required" : unsupported ? "review_required" : failed ? "failed" : (skipped || completedStage) ? "completed" : "review_required";
                string reasonCode = humanRequired ? TypedFailureReason(safeStageDetail, "ww_account_human_required") : unsupported ? (binding.GameId == "WW" ? "ww_account_switch_unsupported" : "upstream_schema_migration_required") : uncertain ? "upstream_result_uncertain" : failed ? TypedFailureReason(safeStageDetail, "upstream_stage_failed") : skipped ? "upstream_task_skipped" : completedStage ? "upstream_task_succeeded" : "upstream_observation_missing";
                Emit(Base(request, binding.GameId, "todo_terminal", sequence++, new Dictionary<string, object> {
                    { "todoInstanceId", id }, { "todoAttemptId", attemptId }, { "status", terminalStatus },
                    { "reasonCode", reasonCode },
                    // An unverified routine stage is not completion, but a
                    // fresh full batch may retry the fixed GUI lifecycle from
                    // the beginning. Human-required outcomes are produced by
                    // the upstream stage bridge and remain non-retryable.
                    { "reason", safeStageDetail }, { "retryable", !humanRequired && !unsupported && !uncertain && (failed || (!skipped && !completedStage)) }, { "evidenceArtifactIds", evidenceArtifactIds.ToArray() }
                }));
                terminal.Add(id);
                if (terminalStatus == "completed") completed.Add(id);
                if (terminalStatus == "failed") failedTodos.Add(id);
            };
            // Keep terminal evidence open until the tool has finished writing
            // its final logs. The task itself continues without waiting on us.
            Action<string, string, string> terminalTodo = (operation, state, terminalDetail) => {
                Tuple<string, IDictionary<string, object>> target;
                if (!todosByOperation.TryGetValue(operation, out target) || !attempted.Contains(target.Item1) || terminal.Contains(target.Item1)) return;
                if (!toolFinished && state != "human_required") {
                    if (pendingTerminalOperations.Add(operation)) {
                        if (state == "completed" || state == "failed" || state == "skipped")
                            Emit(Base(request, binding.GameId, "todo_progress", sequence++, new Dictionary<string, object> {
                                { "todoInstanceId", target.Item1 }, { "todoAttemptId", attempts[target.Item1] },
                                { "code", "upstream_step_returned" },
                                { "message", String.IsNullOrEmpty(terminalDetail) ? "Official step returned; collecting final tool logs." : terminalDetail.Substring(0, Math.Min(terminalDetail.Length, 2000)) },
                                { "metrics", new Dictionary<string, object> {
                                    { "officialStatus", state }, { "operation", operation },
                                    { "waitingFor", "official tool exit and complete log collection" }
                                } }
                            }));
                        pendingTerminals.Add(Tuple.Create(operation, state, terminalDetail));
                    }
                    return;
                }
                emitTerminalTodo(operation, state, terminalDetail);
            };

            Action<string, string> progressTodo = (operation, stageDetail) => {
                // Manager protocol: todo_progress = base fields + todoInstanceId,
                // todoAttemptId, code (identifier) and optional message; it is
                // only valid for a Todo whose attempt has started and is not
                // terminal.  Anything else is a protocol failure for the run.
                Tuple<string, IDictionary<string, object>> target;
                if (!todosByOperation.TryGetValue(operation, out target) || terminal.Contains(target.Item1)) return;
                if (!attempted.Contains(target.Item1)) return;
                string code = "formal_gui_update_progress";
                string message = String.IsNullOrWhiteSpace(stageDetail) ? "formal GUI update still progressing" : stageDetail;
                int separator = message.IndexOf(':');
                if (separator > 0 && separator <= 64) {
                    string candidate = message.Substring(0, separator).Trim();
                    if (Regex.IsMatch(candidate, "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")) code = candidate;
                }
                if (message.Length > 2000) message = message.Substring(0, 2000);
                Emit(Base(request, binding.GameId, "todo_progress", sequence++, new Dictionary<string, object> {
                    { "todoInstanceId", target.Item1 }, { "todoAttemptId", attempts[target.Item1] },
                    { "code", code }, { "message", message }
                }));
            };
            Action<string, string, string> ApplyWwStage = (operation, state, stageDetail) => {
                if (operation == "upstream-daily-task") {
                    upstreamDailyFailed |= state == "failed";
                    if (binding.GameId == "Endfield") {
                        Emit(Base(request, binding.GameId, "run_progress", sequence++, new Dictionary<string, object> {
                            { "operation", operation }, { "code", "official_daily_status" },
                            { "message", stageDetail.Substring(0, Math.Min(stageDetail.Length, 2000)) },
                            { "metrics", new Dictionary<string, object> {
                                { "officialStatus", state }, { "trigger", "Official DailyTask.run" },
                                { "waitingFor", state == "started" ? "Official prerequisite or selected method result" : "Official tool exit and complete log collection" }
                            } }
                        }));
                    }
                    return;
                }
                if (state == "started") {
                    Tuple<string, IDictionary<string, object>> target;
                    bool newStep = todosByOperation.TryGetValue(operation, out target) && !attempted.Contains(target.Item1);
                    startTodo(operation);
                    if (binding.GameId == "Endfield" && newStep) {
                        Emit(Base(request, binding.GameId, "todo_progress", sequence++, new Dictionary<string, object> {
                            { "todoInstanceId", target.Item1 }, { "todoAttemptId", attempts[target.Item1] },
                            { "code", "official_method_entered" }, { "message", stageDetail },
                            { "metrics", new Dictionary<string, object> {
                                { "trigger", "Manager selected this operation and official DailyTask entered its method" },
                                { "waitingFor", "Official method return or exception" }
                            } }
                        }));
                    }
                }
                else if (state == "progress") progressTodo(operation, stageDetail);
                else if (state == "step_artifact") ImportWwSidecarArtifact(wwObservationRead ? observationFile : Path.Combine(staging, "stage.jsonl"), operation, state, stageDetail, wwSidecarStepEvidence);
                else if (state == "skipped") {
                    // The official condition may skip the task body before
                    // a method-enter event exists. Record that decision,
                    // without claiming the task body was executed.
                    startTodo(operation);
                    terminalTodo(operation, state, stageDetail);
                }
                else if (state == "human_required") {
                    startTodo(operation);
                    terminalTodo(operation, state, stageDetail);
                    humanChoiceRequired = true;
                }
                else if (state == "completed" || state == "failed" || state == "unsupported" || state == "uncertain") terminalTodo(operation, state, stageDetail);
            };
            int exitCode; string detail; bool cancelled;
            try {
                if (binding.GameId == "WW" && !binding.WwInspectionOnly && !binding.WwRewardInspectionOnly && !binding.WwBattlePassInspectionOnly && wwInspectionGroups.Count <= 1 && !WwSelectionIsWholeOfficialDaily(todosByOperation.Keys, RequestedSelection(request))) {
                    string unsupportedReason = "ww_selection_unsupported: upstream DailyTask has no official per-step enablement for this partial selection";
                    Emit(Base(request, binding.GameId, "run_progress", sequence++, new Dictionary<string, object> {
                        { "operation", "official-config-validation" }, { "code", "ww_selection_unsupported" },
                        { "message", unsupportedReason },
                        { "metrics", new Dictionary<string, object> {
                            { "trigger", "Selected operations cannot be expressed by the bound official DailyTask configuration" },
                            { "waitingFor", "A supported official task selection before a new run" },
                            { "unstartedOperations", todosByOperation.Keys.ToArray() }
                        } }
                    }));
                    exitCode = 0;
                    detail = unsupportedReason;
                    cancelled = false;
                } else if (binding.GameId == "GF2" && Gf2MainOnlyOperationsSelected(todosByOperation.Keys) && !IsGf2MainSourceBinding(binding)) {
                    const string unsupportedReason = "gf2_main_source_required: selected GF2 operations require the audited official main source binding; the legacy DailyTask combines or lacks these methods";
                    Emit(Base(request, binding.GameId, "run_progress", sequence++, new Dictionary<string, object> {
                        { "operation", "official-config-validation" }, { "code", "gf2_main_source_required" },
                        { "message", unsupportedReason },
                        { "metrics", new Dictionary<string, object> {
                            { "trigger", "Selected operations require methods absent from the bound official version" },
                            { "waitingFor", "Bind the supported official version before a new run" },
                            { "unstartedOperations", todosByOperation.Keys.ToArray() }
                        } }
                    }));
                    exitCode = 0;
                    detail = unsupportedReason;
                    cancelled = false;
                } else exitCode = RunDaily(binding, todosByOperation.Keys.ToArray(), (operation, state, stageDetail) => {
                    if (binding.GameId == "Endfield" && state == "integration") {
                        var integration = Json.DeserializeObject(stageDetail) as IDictionary<string, object>;
                        if (integration == null) throw new InvalidDataException("Invalid Endfield integration event");
                        Emit(Base(request, binding.GameId, "run_progress", sequence++, new Dictionary<string, object> {
                            { "operation", operation }, { "code", integration["code"] },
                            { "message", integration["message"] }, { "metrics", integration["metrics"] }
                        }));
                        return;
                    }
                    if (binding.GameId == "Endfield" && state == "integration_artifact") {
                        string fileName = DetailValue(stageDetail, "file");
                        string kind = DetailValue(stageDetail, "kind");
                        if (String.IsNullOrEmpty(fileName) || Path.GetFileName(fileName) != fileName ||
                            !new[] { "game-ui-login-before-raw", "game-ui-login-after-raw" }.Contains(kind))
                            throw new InvalidDataException("Invalid Endfield login artifact");
                        string imagePath = Path.Combine(staging, fileName);
                        Emit(Base(request, binding.GameId, "run_artifact_staged", sequence++, new Dictionary<string, object> {
                            // Protocol anchor only; a run diagnostic never starts this Todo.
                            { "todoInstanceId", ids[0] },
                            { "artifactId", Guid.NewGuid().ToString() }, { "kind", kind }, { "fileName", fileName },
                            { "mimeType", "image/png" }, { "sizeBytes", new FileInfo(imagePath).Length },
                            { "sha256", Hash(imagePath) }, { "capturedAt", DetailValue(stageDetail, "capturedAt") }
                        }));
                        return;
                    }
                    if ((binding.GameId == "GF2" || binding.GameId == "Endfield") && state == "progress" &&
                        (stageDetail.StartsWith("step_capture_unavailable:before:", StringComparison.Ordinal) ||
                         stageDetail.StartsWith("step_capture_unavailable:after:", StringComparison.Ordinal) ||
                         stageDetail.StartsWith("step_capture_unavailable:recognition:", StringComparison.Ordinal))) {
                        string phase = stageDetail.Split(':')[1];
                        Emit(Base(request, binding.GameId, "run_progress", sequence++, new Dictionary<string, object> {
                            { "operation", operation + "-screenshot-" + phase }, { "code", "step_capture_unavailable" },
                            { "message", stageDetail.Substring(0, Math.Min(stageDetail.Length, 2000)) },
                            { "metrics", new Dictionary<string, object> {
                                { "integrationStatus", "failed" },
                                { "trigger", "Official recognition input: " + operation + ":" + phase },
                                { "waitingFor", "" }
                            } }
                        }));
                        return;
                    }
                    if ((binding.GameId == "GF2" || binding.GameId == "Endfield") && state == "step_artifact") {
                        Tuple<string, IDictionary<string, object>> target;
                        if (!todosByOperation.TryGetValue(operation, out target) || !attempted.Contains(target.Item1)) return;
                        var descriptor = ParseStepArtifactDescriptor(binding.GameId, binding.Gf2RewardInspectionOnly, stageDetail);
                        if (descriptor == null) return;
                        string fileName = descriptor.Item1;
                        string kind = descriptor.Item2;
                        string imagePath = Path.Combine(staging, fileName);
                        if (!File.Exists(imagePath)) return;
                        Emit(Base(request, binding.GameId, "artifact_staged", sequence++, new Dictionary<string, object> {
                            { "todoInstanceId", target.Item1 }, { "todoAttemptId", attempts[target.Item1] },
                            { "artifactId", Guid.NewGuid().ToString() }, { "kind", kind }, { "fileName", fileName },
                            { "mimeType", descriptor.Item3 }, { "sizeBytes", new FileInfo(imagePath).Length },
                            { "sha256", Hash(imagePath) }, { "capturedAt", DetailValue(stageDetail, "capturedAt") }
                        }));
                        return;
                    }
                    if (state == "tool_started") {
                        if (binding.GameId == "WW") {
                            // After a cached account replay, supplementary
                            // official entries produce new files in staging.
                            // They must not import from or overwrite the shared
                            // ordinary multi-account observation cache.
                            if (wwLiveToolStarted || wwObservationRead) {
                                wwObservationRead = false;
                                wwObservationWrite = false;
                            }
                            wwLiveToolStarted = true;
                        }
                        if (binding.GameId == "WW" && binding.WwInspectionOperations.Count > 1) {
                            // Each fixed official invocation must confirm its
                            // saved account independently before attribution.
                            officialCurrentAccount = String.Empty;
                            pendingUntaggedStages.Clear();
                        }
                        Emit(Base(request, binding.GameId, "run_progress", sequence++, new Dictionary<string, object> {
                            { "operation", "official-tool-start" }, { "code", "waiting_for_official_task" },
                            { "message", stageDetail },
                            { "metrics", new Dictionary<string, object> {
                                { "trigger", "Manager selected the official task through its CLI" },
                                { "waitingFor", "Official method-enter event or tool process exit" }
                            } }
                        }));
                        return;
                    }
                    if (binding.GameId == "WW" && (operation == "official-login-input" || operation == "login-window-activation")) {
                        Emit(Base(request, binding.GameId, "run_progress", sequence++, new Dictionary<string, object> {
                            { "operation", operation }, { "code", operation == "official-login-input" ? "official_log_record" : "login_window_activation" },
                            { "message", stageDetail.Substring(0, Math.Min(stageDetail.Length, 2000)) },
                            { "metrics", new Dictionary<string, object> {
                                { "trigger", operation == "official-login-input" ? "Official wait_login called PostMessage.update_mouse_pos" : "Official wait_login selected native dialog Button while game is not foreground" },
                                { "waitingFor", "Official login state change; input target selection does not prove login" }
                            } }
                        }));
                        return;
                    }
                    if (binding.GameId == "WW" && operation == "upstream-multi-account-task") {
                        upstreamDailyFailed |= state == "failed";
                        Emit(Base(request, binding.GameId, "run_progress", sequence++, new Dictionary<string, object> {
                            { "operation", operation }, { "code", "official_multi_account_status" },
                            { "message", stageDetail.Substring(0, Math.Min(stageDetail.Length, 2000)) },
                            { "metrics", new Dictionary<string, object> {
                                { "officialStatus", state }, { "trigger", "Official MultiAccountDailyTask.run" },
                                { "waitingFor", state == "started" ? "Official multi-account task result" : "Official tool exit and complete log collection" }
                            } }
                        }));
                        return;
                    }
                    string eventAccount = DetailValue(stageDetail, "account");
                    if (operation == "official-account") {
                        if (state == "detected" && !String.IsNullOrEmpty(eventAccount)) {
                            officialCurrentAccount = eventAccount;
                            var pending = pendingUntaggedStages.ToArray();
                            pendingUntaggedStages.Clear();
                            if (WwAccountsMatch(eventAccount, binding.SavedAccountLabel)) {
                                foreach (var item in pending) ApplyWwStage(item.Item1, item.Item2, item.Item3);
                            } else if (wwObservationWrite) {
                                foreach (var item in pending)
                                    AppendWwObservation(observationFile, item.Item1, item.Item2, item.Item3, eventAccount);
                            }
                        }
                        return;
                    }
                    if (wwObservationWrite) AppendWwObservation(observationFile, operation, state, stageDetail, eventAccount);
                    if (wwObservationRead && (state == "sidecar_before" || state == "sidecar_after" || state == "sidecar_after_watermarked")) {
                        ImportWwSidecarArtifact(observationFile, operation, state, stageDetail, wwSidecarStepEvidence);
                        return;
                    }
                    if (binding.AccountEnabled && (wwObservationWrite || wwObservationRead)) {
                        if (state == "step_artifact" && DetailValue(stageDetail, "accountAttribution") == "unconfirmed") {
                            // Recognition input copied before an official
                            // account identity is diagnostic forever. A later
                            // login must not attach it to pending account steps.
                            Tuple<string, IDictionary<string, object>> target;
                            string fileName = DetailValue(stageDetail, "file");
                            string kind = DetailValue(stageDetail, "kind");
                            if (todosByOperation.TryGetValue(operation, out target) && IsStepArtifactKind(kind) &&
                                !String.IsNullOrEmpty(fileName) && fileName == Path.GetFileName(fileName)) {
                                string artifactPath = Path.Combine(staging, fileName);
                                if (File.Exists(artifactPath))
                                    Emit(Base(request, binding.GameId, "run_artifact_staged", sequence++, new Dictionary<string, object> {
                                        { "todoInstanceId", target.Item1 }, { "artifactId", Guid.NewGuid().ToString() },
                                        { "kind", kind }, { "fileName", fileName }, { "mimeType", StepArtifactMimeType(kind) },
                                        { "sizeBytes", new FileInfo(artifactPath).Length }, { "sha256", Hash(artifactPath) },
                                        { "capturedAt", DetailValue(stageDetail, "capturedAt") }
                                    }));
                            }
                            return;
                        }
                        if (String.IsNullOrEmpty(eventAccount)) {
                            if (String.IsNullOrEmpty(officialCurrentAccount)) {
                                pendingUntaggedStages.Add(Tuple.Create(operation, state, stageDetail));
                                if (!wwUnattributedStartupPublished && operation == "attach-world" && state == "step_artifact" &&
                                    (DetailValue(stageDetail, "kind") == "game-ui-step-before-raw" ||
                                     DetailValue(stageDetail, "kind") == "game-ui-recognition-raw")) {
                                    Tuple<string, IDictionary<string, object>> target;
                                    string fileName = DetailValue(stageDetail, "file");
                                    if (todosByOperation.TryGetValue(operation, out target) && !String.IsNullOrEmpty(fileName) && fileName == Path.GetFileName(fileName)) {
                                        string imagePath = Path.Combine(staging, fileName);
                                        if (File.Exists(imagePath)) {
                                            Emit(Base(request, binding.GameId, "run_artifact_staged", sequence++, new Dictionary<string, object> {
                                                { "todoInstanceId", target.Item1 }, { "artifactId", Guid.NewGuid().ToString() },
                                                { "kind", "game-ui-account-unconfirmed-before-raw" }, { "fileName", fileName },
                                                { "mimeType", "image/png" }, { "sizeBytes", new FileInfo(imagePath).Length },
                                                { "sha256", Hash(imagePath) }, { "capturedAt", DetailValue(stageDetail, "capturedAt") }
                                            }));
                                            wwUnattributedStartupPublished = true;
                                        }
                                    }
                                }
                                Emit(Base(request, binding.GameId, "run_progress", sequence++, new Dictionary<string, object> {
                                    { "operation", operation }, { "code", "official_account_pending" },
                                    { "message", stageDetail.Substring(0, Math.Min(stageDetail.Length, 1800)) + "; official account attribution pending" },
                                    { "metrics", new Dictionary<string, object> {
                                        { "officialStatus", state }, { "accountAttribution", "pending" },
                                        { "trigger", "Official task emitted this stage before detecting the current account" },
                                        { "waitingFor", "Official MultiAccountDailyTask account identity; no account completion inferred" }
                                    } }
                                }));
                                return;
                            }
                            eventAccount = officialCurrentAccount;
                        }
                        if (!WwAccountsMatch(eventAccount, binding.SavedAccountLabel)) {
                            return;
                        }
                    }
                    ApplyWwStage(operation, state, stageDetail);
                }, () => cancellationRequested.WaitOne(0), wwObservationRead, observationFile, out detail, out cancelled);
            }
            catch (Exception error) { exitCode = -1; detail = "tool_start_failed: " + error.Message; cancelled = false; }
            toolFinished = true;
            foreach (var pending in pendingTerminals) {
                emitTerminalTodo(pending.Item1, pending.Item2, pending.Item3);
            }
            // Unobserved children remain unresolved and unattempted, even if
            // the root DailyTask returned successfully. Do not invent starts
            // or take "before" screenshots after the task has already exited.
            if (!cancelled && !humanChoiceRequired) {
                foreach (string operation in todosByOperation.Keys) {
                    Tuple<string, IDictionary<string, object>> target = todosByOperation[operation];
                    if (attempted.Contains(target.Item1) && !terminal.Contains(target.Item1))
                        terminalTodo(operation, exitCode == 0 ? "review_required" : "failed", detail + "; stage_event_missing");
                }
            }
            // A run which never started one of the configured Todos still
            // produced upstream logs: a multi-account task may fail before it
            // ever reports account identity, so nothing above attributes those
            // stages to a Todo and the raw log would stay in the staging inbox.
            // Archive the remainder as run diagnostics so a failed round keeps
            // its complete upstream evidence.  A run diagnostic never starts,
            // completes or fails a Todo.
            foreach (string upstreamLog in Directory.GetFiles(staging, binding.GameId.ToLowerInvariant() + "-upstream-*.jsonl")
                         .Where(value => !Path.GetFileName(value).Contains("-snapshot-") && !stagedUpstreamLogs.Contains(value))) {
                FileInfo upstreamInfo = new FileInfo(upstreamLog);
                if (upstreamInfo.Length == 0) continue;
                string snapshotName = binding.GameId.ToLowerInvariant() + "-upstream-snapshot-" + Guid.NewGuid().ToString("N") + ".jsonl";
                string snapshotPath = Path.Combine(staging, snapshotName);
                File.Copy(upstreamLog, snapshotPath, true);
                FileInfo snapshotInfo = new FileInfo(snapshotPath);
                stagedUpstreamLogs.Add(upstreamLog);
                Emit(Base(request, binding.GameId, "run_artifact_staged", sequence++, new Dictionary<string, object> {
                    // Protocol anchor only; a run diagnostic never starts this Todo.
                    { "todoInstanceId", ids[0] },
                    { "artifactId", Guid.NewGuid().ToString() }, { "kind", "tool-log-upstream" },
                    { "fileName", snapshotName }, { "mimeType", "text/plain" },
                    { "sizeBytes", snapshotInfo.Length }, { "sha256", Hash(snapshotPath) },
                    { "capturedAt", upstreamInfo.LastWriteTimeUtc.ToString("o") }
                }));
            }
            // A normal process exit does not erase an official task failure.
            // Keep transport outcome and business outcome independent.
            bool transportClean = exitCode == 0;
            bool allCompleted = completed.Count == ids.Length && transportClean;
            bool humanStopConfirmed = humanChoiceRequired && exitCode == 30;
            string runStatus = ResolveRunStatus(cancelled, upstreamDailyFailed || failedTodos.Count > 0, allCompleted, transportClean, humanStopConfirmed);
            int declaredExitCode = exitCode;
            string[] attemptedIds = ids.Cast<string>().Where(id => attempted.Contains(id)).ToArray();
            string[] completedIds = ids.Cast<string>().Where(id => completed.Contains(id)).ToArray();
            string[] unresolvedIds = ids.Cast<string>().Where(id => !completed.Contains(id)).ToArray();
            Emit(Base(request, binding.GameId, "run_terminal", sequence++, new Dictionary<string, object> {
                { "status", runStatus }, { "transportOutcome", cancelled ? "cancelled" : (transportClean || humanStopConfirmed ? "clean" : "crashed") },
                { "attemptedTodoInstanceIds", attemptedIds }, { "completedTodoInstanceIds", completedIds }, { "unresolvedTodoInstanceIds", unresolvedIds },
                { "terminalEventDigest", TerminalDigest(request, runStatus, attemptedIds, completedIds, unresolvedIds, declaredExitCode) }, { "exitCode", declaredExitCode }
            }));
            // The Host compares the process exit with run_terminal.  Returning
            // zero here used to replace a precise upstream failure with the
            // generic process_exit_mismatch error.
            return declaredExitCode;
        }

        private static string ResolveRunStatus(bool cancelled, bool officialFailed, bool allCompleted, bool transportClean, bool humanStopConfirmed) {
            if (cancelled) return "cancelled";
            if (humanStopConfirmed) return "human_required";
            if (officialFailed || !transportClean) return "failed";
            return allCompleted ? "completed" : "review_required";
        }

        private static Dictionary<string, string> ParseArguments(string[] args) {
            if (args == null || args.Length != 8) return null;
            var result = new Dictionary<string, string>(StringComparer.Ordinal);
            for (int i = 0; i < args.Length; i += 2) { if (!args[i].StartsWith("--") || result.ContainsKey(args[i])) return null; result.Add(args[i], args[i + 1]); }
            return result.Count == 4 && result.ContainsKey("--protocol-version") && result.ContainsKey("--run-id") && result.ContainsKey("--run-attempt-id") && result.ContainsKey("--game-id") ? result : null;
        }
        private static bool SupportedGame(string gameId) { return gameId == "WW" || gameId == "Endfield" || gameId == "GF2"; }
        private static bool MatchesRequest(IDictionary<string, object> r, Dictionary<string, string> o) {
            return r != null && (r["protocolVersion"] as string) == ProtocolVersion && (r["gameId"] as string) == o["--game-id"] &&
                (r["runId"] as string) == o["--run-id"] && (r["runAttemptId"] as string) == o["--run-attempt-id"] && r["preserveClientOnStop"] is bool && (bool)r["preserveClientOnStop"] && ValidAccountRequest(r);
        }
        private static bool ValidAccountRequest(IDictionary<string, object> request) {
            if (!request.ContainsKey("accountId") && !request.ContainsKey("accountSnapshot")) return true;
            if ((request["gameId"] as string) != "WW") return false;
            string id = request.ContainsKey("accountId") ? request["accountId"] as string : "default";
            Guid parsed;
            if (id != "default" && (!Guid.TryParseExact(id, "D", out parsed) || parsed.ToString("D") != id)) return false;
            if (!request.ContainsKey("accountSnapshot")) return id == "default";
            IDictionary<string, object> snapshot = request["accountSnapshot"] as IDictionary<string, object>;
            if (snapshot == null) return false;
            if (snapshot.Count == 0) return id == "default";
            if (snapshot.Count != 2 || !snapshot.ContainsKey("label") || !snapshot.ContainsKey("saved_account_label")) return false;
            foreach (string key in new [] { "label", "saved_account_label" }) {
                string value = snapshot[key] as string;
                if (String.IsNullOrWhiteSpace(value) || value != value.Trim() || value.Length > (key == "label" ? 80 : 160) || value.Any(ch => Char.IsControl(ch) || ch == '\\' || ch == '/')) return false;
            }
            return ((string)snapshot["saved_account_label"]).Contains("****");
        }
        private static void ConfigureAccountBinding(Binding binding, IDictionary<string, object> request, object[] todos) {
            if (!request.ContainsKey("accountSnapshot")) return;
            IDictionary<string, object> snapshot = request["accountSnapshot"] as IDictionary<string, object>;
            if (snapshot == null || snapshot.Count == 0) return;
            binding.AccountId = request.ContainsKey("accountId") ? (string)request["accountId"] : "default";
            // An empty label is current-client mode. A saved label selects the
            // official MultiAccountDailyTask entry; switching stays upstream.
            if (String.IsNullOrWhiteSpace(snapshot["saved_account_label"] as string)) return;
            binding.AccountSnapshot = snapshot;
            binding.AccountOperation = ((IDictionary<string, object>)todos[0])["operation"] as string;
        }
        private static bool MatchesCancel(IDictionary<string, object> control, IDictionary<string, object> request) {
            return control != null && request != null && (control["protocolVersion"] as string) == ProtocolVersion &&
                (control["controlType"] as string) == "cancel" && (control["runId"] as string) == (request["runId"] as string) &&
                (control["runAttemptId"] as string) == (request["runAttemptId"] as string) &&
                (control["fencingToken"] as string) == (request["fencingToken"] as string);
        }
        private static bool AllowedOperation(string gameId, string operation) {
            if (gameId == "WW") return operation == "attach-world" || operation == "inspect-daily-progress" || operation == "farm-nightmare-daily-echo" || operation == "spend-waveplates" || operation == "claim-daily-reward" || operation == "claim-mail" || operation == "claim-battle-pass";
            if (gameId == "Endfield") return operation == "attach-world" || operation == "mail" || operation == "spend-sanity" || operation == "delivery-commission" || operation == "collect-credit" || operation == "dijiang-harvest" || operation == "claim-daily-reward";
            return operation == "attach-home" || operation == "mail" || operation == "activity-supply" || operation == "star-wish" || operation == "event-battle" || operation == "exploration-claim" || operation == "public-area-dispatch" || operation == "spend-stamina" || operation == "squad-tasks" || operation == "claim-daily-missions" || operation == "battle-pass-free-track";
        }
        private static bool EndfieldShouldUseRewardInspection(string gameId, string accountId,
            IEnumerable<string> operations, IDictionary<string, object> request, bool supported) {
            object value;
            if (!request.TryGetValue("observationOnlyOperations", out value)) return false;
            var observations = value as object[];
            if (observations != null && observations.Length == 0) return false;
            var selected = new HashSet<string>(operations ?? Enumerable.Empty<string>(), StringComparer.Ordinal);
            if (gameId != "Endfield" || accountId != "default" || !supported || observations == null ||
                observations.Length != 1 || (observations[0] as string) != "claim-daily-reward" ||
                !selected.Contains("claim-daily-reward") ||
                !selected.IsSubsetOf(new [] { "attach-world", "claim-daily-reward" }))
                throw new InvalidDataException("endfield_readonly_scope_invalid");
            return true;
        }

        private static bool Gf2ShouldUseRewardInspection(string accountId, IEnumerable<string> operations,
            IDictionary<string, object> request, bool supported) {
            object raw;
            if (!request.TryGetValue("observationOnlyOperations", out raw)) return false;
            var observations = raw as object[];
            var selected = new HashSet<string>(operations ?? Enumerable.Empty<string>(), StringComparer.Ordinal);
            var allowed = new HashSet<string>(new[] { "claim-daily-missions", "battle-pass-free-track" }, StringComparer.Ordinal);
            selected.Remove("attach-home");
            if (!supported || accountId != "default" || observations == null || observations.Length < 1 || observations.Length > 2 ||
                observations.Any(item => !(item is string) || !allowed.Contains((string)item)) ||
                observations.Distinct().Count() != observations.Length ||
                !selected.SetEquals(observations.Cast<string>()))
                throw new InvalidDataException("gf2_readonly_scope_invalid");
            return true;
        }

        private static bool EndfieldInspectionSourceMatches(string root, IDictionary<string, object> source) {
            string policyPath = Path.Combine(Path.GetDirectoryName(typeof(Program).Assembly.Location), "EndfieldInspectionContract.json");
            if (source == null || !File.Exists(policyPath)) return false;
            var policy = Json.DeserializeObject(File.ReadAllText(policyPath, Encoding.UTF8)) as IDictionary<string, object>;
            if (policy == null || !NumberEquals(policy["schemaVersion"], 1) ||
                (source["commit"] as string) != (policy["commit"] as string) ||
                !source.ContainsKey("officialBaseCommit") ||
                (source["officialBaseCommit"] as string) != (policy["officialBaseCommit"] as string) ||
                (source["repository"] as string) != (policy["repository"] as string)) return false;
            string archive = Path.Combine(root, "official-source.zip");
            if (!File.Exists(archive) || Hash(archive) != (policy["archiveSha256"] as string)) return false;
            string working = Path.GetFullPath(Path.Combine(root, "Endfield", "source")) + Path.DirectorySeparatorChar;
            var files = policy["files"] as IDictionary<string, object>;
            if (files == null || files.Count != 6) return false;
            foreach (var file in files) {
                string path = Path.GetFullPath(Path.Combine(working, file.Key.Replace('/', Path.DirectorySeparatorChar)));
                if (!path.StartsWith(working, StringComparison.OrdinalIgnoreCase) || !File.Exists(path) ||
                    (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0 || Hash(path) != (file.Value as string)) return false;
            }
            return true;
        }
        private static readonly HashSet<string> WwOfficialDailyOperations = new HashSet<string>(StringComparer.Ordinal) {
            "attach-world", "inspect-daily-progress", "farm-nightmare-daily-echo", "spend-waveplates",
            "claim-daily-reward", "claim-mail", "claim-battle-pass"
        };
        // Frozen account selection supplied by the Manager.  Absent for older
        // Managers and for the current-client scope.
        private static HashSet<string> RequestedSelection(IDictionary<string, object> request) {
            var selected = new HashSet<string>(StringComparer.Ordinal);
            object raw;
            if (request == null || !request.TryGetValue("selectedOperations", out raw)) return selected;
            var items = raw as IEnumerable<object>;
            if (items == null) return selected;
            foreach (object item in items) {
                string value = item as string;
                if (!String.IsNullOrWhiteSpace(value)) selected.Add(value);
            }
            return selected;
        }
        private static bool WwSelectionIsWholeOfficialDaily(IEnumerable<string> operations, HashSet<string> selected) {
            var actual = new HashSet<string>(operations ?? Enumerable.Empty<string>(), StringComparer.Ordinal);
            // ensure_main/open_daily are mandatory official prerequisites and
            // can be called again after their Todo was already completed. They
            // are not spending/claiming actions. All official task-body actions
            // must still be runnable: a completed claim must never be replayed
            // merely because the frozen user selection contains it.
            var body = new HashSet<string>(WwOfficialDailyOperations, StringComparer.Ordinal);
            body.ExceptWith(new[] { "attach-world", "inspect-daily-progress" });
            if (!actual.IsSubsetOf(WwOfficialDailyOperations) || !body.IsSubsetOf(actual)) return false;
            return selected == null || selected.Count == 0 || WwOfficialDailyOperations.IsSubsetOf(selected);
        }
        private static bool WwShouldUseInspection(IEnumerable<string> operations, HashSet<string> selected, bool supported) {
            var actual = new HashSet<string>(operations ?? Enumerable.Empty<string>(), StringComparer.Ordinal);
            return supported && actual.IsSubsetOf(new[] { "attach-world", "inspect-daily-progress" }) && actual.Contains("inspect-daily-progress")
                && !WwSelectionIsWholeOfficialDaily(actual, selected);
        }
        private static List<string[]> WwInspectionGroups(Binding binding, string[] operations) {
            var result = new List<string[]>();
            var actual = new HashSet<string>(operations ?? new string[0], StringComparer.Ordinal);
            if (binding.GameId != "WW" || actual.Count != (operations ?? new string[0]).Length ||
                !actual.IsSubsetOf(new[] { "attach-world", "inspect-daily-progress", "claim-daily-reward", "claim-battle-pass" })) return result;
            var supported = new Dictionary<string, bool> {
                { "inspect-daily-progress", binding.WwInspectionSupported },
                { "claim-daily-reward", binding.WwRewardInspectionSupported },
                { "claim-battle-pass", binding.WwBattlePassInspectionSupported }
            };
            foreach (string operation in new[] { "inspect-daily-progress", "claim-daily-reward", "claim-battle-pass" }) {
                if (!actual.Contains(operation)) continue;
                if (!supported[operation]) return new List<string[]>();
                result.Add(actual.Contains("attach-world") ? new[] { "attach-world", operation } : new[] { operation });
            }
            return result;
        }
        private static bool WwShouldUseRewardInspection(IEnumerable<string> operations, bool supported) {
            var actual = new HashSet<string>(operations ?? Enumerable.Empty<string>(), StringComparer.Ordinal);
            return supported && actual.Contains("claim-daily-reward")
                && actual.IsSubsetOf(new[] { "attach-world", "claim-daily-reward" });
        }
        private static bool WwShouldUseBattlePassInspection(IEnumerable<string> operations, bool supported) {
            var actual = new HashSet<string>(operations ?? Enumerable.Empty<string>(), StringComparer.Ordinal);
            return supported && actual.Contains("claim-battle-pass")
                && actual.IsSubsetOf(new[] { "attach-world", "claim-battle-pass" });
        }
        private static readonly HashSet<string> Gf2MainOnlyOperations = new HashSet<string>(StringComparer.Ordinal) {
            "activity-supply", "star-wish", "event-battle", "exploration-claim"
        };
        private static bool Gf2MainOnlyOperationsSelected(IEnumerable<string> operations) {
            return (operations ?? Enumerable.Empty<string>()).Any(Gf2MainOnlyOperations.Contains);
        }
        private static bool IsGf2MainSourceBinding(Binding binding) {
            return binding != null && String.Equals(binding.AppName, "ok-gf2-official-source-candidate", StringComparison.Ordinal);
        }
        private static Binding LoadBinding(string gameId) {
            string path = Environment.GetEnvironmentVariable("YEYU_GAMER_INSTALLATION_BINDING_PATH");
            if (!String.IsNullOrEmpty(path)) return LoadManagerBinding(path, gameId);
            string root = AppDomain.CurrentDomain.BaseDirectory;
            IDictionary<string, object> doc = Json.DeserializeObject(File.ReadAllText(Path.Combine(root, "tool-binding.json"), Encoding.UTF8)) as IDictionary<string, object>;
            IDictionary<string, object> tools = doc == null ? null : doc["tools"] as IDictionary<string, object>;
            IDictionary<string, object> tool = tools == null ? null : tools[gameId] as IDictionary<string, object>;
            if (tool == null || !NumberEquals(doc["schemaVersion"], 2)) throw new InvalidOperationException("binding_scope_invalid");
            return MakeFormalGuiBinding(gameId, tool["game"] as string, tool["root"] as string, null);
        }
        private static Binding LoadManagerBinding(string path, string expectedGameId) {
            string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            string full = Path.GetFullPath(path);
            if (String.IsNullOrEmpty(staging) || !Path.IsPathRooted(full) || full.StartsWith("\\\\") || !File.Exists(full) ||
                !String.Equals(Path.GetFileName(full), "installation-binding.json", StringComparison.OrdinalIgnoreCase) ||
                !full.StartsWith(Path.GetFullPath(staging).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("manager_installation_binding_invalid");
            IDictionary<string, object> doc = Json.DeserializeObject(File.ReadAllText(full, Encoding.UTF8)) as IDictionary<string, object>;
            bool versionOne = NumberEquals(doc == null ? null : doc["schemaVersion"], 1);
            bool versionTwo = NumberEquals(doc == null ? null : doc["schemaVersion"], 2);
            bool validVersionOne = versionOne && doc.Count == 4;
            bool validVersionTwo = versionTwo && (expectedGameId == "WW" || expectedGameId == "Endfield" || expectedGameId == "GF2") && doc.Count == 5 && doc["dailyTaskProfile"] is IDictionary<string, object>;
            if (doc == null || (!validVersionOne && !validVersionTwo) || (doc["gameId"] as string) != expectedGameId || !(doc["gamePath"] is string) || !(doc["toolPath"] is string)) throw new InvalidOperationException("manager_installation_binding_invalid");
            string game = doc["gamePath"] as string; string root = doc["toolPath"] as string;
            if (!Path.IsPathRooted(game) || !Path.IsPathRooted(root) || game.StartsWith("\\\\") || root.StartsWith("\\\\")) throw new InvalidOperationException("configured_installation_missing");
            return MakeFormalGuiBinding(expectedGameId, game, root,
                doc.ContainsKey("dailyTaskProfile") ? doc["dailyTaskProfile"] as IDictionary<string, object> : null);
        }
        private static Binding MakeFormalGuiBinding(string gameId, string game, string root, IDictionary<string, object> dailyTaskProfile) {
            if (gameId == "WW") {
                Binding officialSourceCandidate = TryMakeWwOfficialSourceCandidateBinding(game, root, dailyTaskProfile);
                if (officialSourceCandidate != null) return officialSourceCandidate;
                string working = Path.Combine(root, "data", "apps", "ok-ww", "working");
                Binding binding = MakeBinding(gameId, game, root, Path.Combine(root, "data", "apps", "ok-ww", "python", "pythonw.exe"),
                    "\"" + Path.Combine(working, "main.py") + "\" --task 1 --exit", Path.Combine(working, "configs", "DailyTask.json"), dailyTaskProfile, working);
                binding.AppName = "ok-ww";
                binding.UpdateState = Path.Combine(root, "data", "apps", "ok-ww", "app.json");
                binding.InnerPython = Path.Combine(root, "data", "apps", "ok-ww", "python", "pythonw.exe");
                binding.InnerEntry = Path.Combine(working, "main.py");
                if (!File.Exists(binding.UpdateState) || !File.Exists(binding.InnerPython) || !File.Exists(binding.InnerEntry)) throw new InvalidOperationException("configured_gui_entry_missing");
                return binding;
            }
            if (gameId == "Endfield") {
                Binding officialSourceCandidate = TryMakeEndfieldOfficialSourceCandidateBinding(game, root, dailyTaskProfile);
                if (officialSourceCandidate != null) return officialSourceCandidate;
                string appRoot = Path.Combine(root, "data", "apps", "ok-ef");
                string endfieldWorkingDirectory = Path.Combine(appRoot, "working");
                Binding binding = MakeBinding(gameId, game, root, Path.Combine(appRoot, "python", "pythonw.exe"),
                    "\"" + Path.Combine(endfieldWorkingDirectory, "main.py") + "\" --task 1 --exit", Path.Combine(endfieldWorkingDirectory, "configs", "DailyTask.json"), dailyTaskProfile, endfieldWorkingDirectory);
                binding.AppName = "ok-ef";
                binding.UpdateState = Path.Combine(appRoot, "app.json");
                binding.InnerPython = Path.Combine(appRoot, "python", "pythonw.exe");
                binding.InnerEntry = Path.Combine(endfieldWorkingDirectory, "main.py");
                if (!File.Exists(binding.UpdateState) || !File.Exists(binding.InnerPython) || !File.Exists(binding.InnerEntry)) throw new InvalidOperationException("configured_gui_entry_missing");
                return binding;
            }
            if (gameId == "GF2") {
                Binding officialSourceCandidate = TryMakeGf2OfficialSourceCandidateBinding(game, root, dailyTaskProfile);
                if (officialSourceCandidate != null) return officialSourceCandidate;
            }
            string workingDirectory = Path.Combine(root, "working");
            return MakeBinding(gameId, game, root, Path.Combine(root, "python", "pythonw.exe"),
                "\"" + Path.Combine(workingDirectory, "main.py") + "\" --task 1 --exit", Path.Combine(workingDirectory, "configs", "DailyTask.json"), dailyTaskProfile, workingDirectory);
        }
        // WW candidates use a pinned, prepared source tree.  Manifest fields
        // are validated as provenance only: the fixed layout below supplies
        // both the Python entry point and its command line.
        private static Binding TryMakeWwOfficialSourceCandidateBinding(string game, string root, IDictionary<string, object> dailyTaskProfile) {
            if (String.IsNullOrWhiteSpace(root) || root.StartsWith("\\\\")) return null;
            string candidateRoot = Path.GetFullPath(root); string recordPath = Path.Combine(candidateRoot, "WW", "runtime-preparation.json"); if (!File.Exists(recordPath)) return null;
            IDictionary<string, object> record = Json.DeserializeObject(File.ReadAllText(recordPath, Encoding.UTF8)) as IDictionary<string, object>;
            IDictionary<string, object> source = record == null ? null : record["source"] as IDictionary<string, object>; IDictionary<string, object> archive = source == null ? null : source["archive"] as IDictionary<string, object>; IDictionary<string, object> entry = record == null ? null : record["entry"] as IDictionary<string, object>;
            string expectedWorking = Path.Combine(candidateRoot, "WW", "source"); string expectedPythonw = Path.Combine(candidateRoot, "r", "Scripts", "pythonw.exe"); string expectedMain = Path.Combine(expectedWorking, "main.py"); string expectedArchive = Path.Combine(candidateRoot, "official-source.zip"); string expectedDailyConfig = Path.Combine(expectedWorking, "configs", "DailyTask.json");

            bool inspectionPatch = record != null && source != null && (record["candidateKind"] as string) == "isolated-local-ww-source-patch" &&
                (source["commit"] as string) == "1a779fbf3a2211342c6c83f06011b534cbb80466" && source.ContainsKey("officialBaseCommit") &&
                (source["officialBaseCommit"] as string) == "6ffaa7f0b0b7f3140367de96c8eacba533c96894" &&
                Hash(Path.Combine(expectedWorking, "src", "task", "DailyTask.py")) == "b5f3fb78b095c2a23c17911f10706a565c9c22ead690ed8f6f799cb46452b19b" &&
                Hash(Path.Combine(expectedWorking, "src", "task", "MultiAccountDailyTask.py")) == "2280a301ea9315631a96edabbaf88b775a6d2c3ac4e2dc409c82982321685c45";
            bool rewardInspectionPatch = record != null && source != null && (record["candidateKind"] as string) == "isolated-local-ww-source-patch" &&
                (source["commit"] as string) == "da98151e6bcd2c4f0c191e80c059f84c5e6c4939" && source.ContainsKey("officialBaseCommit") &&
                (source["officialBaseCommit"] as string) == "6ffaa7f0b0b7f3140367de96c8eacba533c96894" &&
                Hash(Path.Combine(expectedWorking, "src", "task", "DailyTask.py")) == "f51aa3aa9a6e614f9bd2a2341dd19a3a10e571ce4be685d372bd513c94cf275c" &&
                Hash(Path.Combine(expectedWorking, "src", "task", "MultiAccountDailyTask.py")) == "94a90a9dac2ff0f92134af3fed1c083d8a61f5684e57b93309401fdf8f60cea1" &&
                Hash(Path.Combine(expectedWorking, "src", "task", "resources", "daily_reward_claimed_check.png")) == "57ef1470aaf2e9d8fb55773d06f14b693a076ea5dc668f685e09b3a83223fd3f";
            bool battlePassInspectionPatch = record != null && source != null && (record["candidateKind"] as string) == "isolated-local-ww-source-patch" &&
                (source["commit"] as string) == "274346512f904b2e2bc2dea78a45982fa0012e83" && source.ContainsKey("officialBaseCommit") &&
                (source["officialBaseCommit"] as string) == "6ffaa7f0b0b7f3140367de96c8eacba533c96894" &&
                Hash(Path.Combine(expectedWorking, "src", "task", "DailyTask.py")) == "84f52efb8b61ea1bb24a0e972ab510ffd4f048786af0a94c821e7fec37c92463" &&
                Hash(Path.Combine(expectedWorking, "src", "task", "MultiAccountDailyTask.py")) == "5ccee3872e8190b94490d9b7329aee6920706d3ab79bef4923595217b54ec0ed" &&
                Hash(Path.Combine(expectedWorking, "src", "task", "resources", "daily_reward_claimed_check.png")) == "57ef1470aaf2e9d8fb55773d06f14b693a076ea5dc668f685e09b3a83223fd3f";
            bool battlePassHistoryPatch = record != null && source != null && (record["candidateKind"] as string) == "isolated-local-ww-source-patch" &&
                source.ContainsKey("officialBaseCommit") &&
                (source["officialBaseCommit"] as string) == "6ffaa7f0b0b7f3140367de96c8eacba533c96894" &&
                (((source["commit"] as string) == "93e19b4f455390f50b8e126c23cf4f4be1595adb" && Hash(Path.Combine(expectedWorking, "src", "task", "DailyTask.py")) == "0805055d5838b99318b052550666f86b4bade94b400a553d98f21ece53b54198") ||
                 ((source["commit"] as string) == "cc2e2e7f32d25b5af269c4b16d063d259ce6cffb" && Hash(Path.Combine(expectedWorking, "src", "task", "DailyTask.py")) == "e673110270a9023e2608c58272897c40da9819d54fe384ee5b110a9dc461fee6") ||
                 ((source["commit"] as string) == "81edb5af888688be64e879f346b0f2440bacd958" && Hash(Path.Combine(expectedWorking, "src", "task", "DailyTask.py")) == "81fd98025c06507c5547e2b8c76fe23ef9d4e12c8fae20c53ca0ac6dc653e828") ||
                 ((source["commit"] as string) == "85e54379b3a6859f30f2a1df6326091f3a51929d" && Hash(Path.Combine(expectedWorking, "src", "task", "DailyTask.py")) == "facab02135deab404e7d59602655c91d915b7ef49a3f74506baf42332f524b5b")) &&
                Hash(Path.Combine(expectedWorking, "src", "task", "MultiAccountDailyTask.py")) == "5ccee3872e8190b94490d9b7329aee6920706d3ab79bef4923595217b54ec0ed" &&
                Hash(Path.Combine(expectedWorking, "src", "task", "resources", "daily_reward_claimed_check.png")) == "57ef1470aaf2e9d8fb55773d06f14b693a076ea5dc668f685e09b3a83223fd3f" &&
                IsAuditedWwCaptureSdkPatch(candidateRoot, record);
            bool currentStatePatch = record != null && source != null &&
                IsWwHardcapCohortSource(expectedWorking, record) &&
                (record["candidateKind"] as string) == "isolated-local-ww-source-patch" &&
                source.ContainsKey("officialBaseCommit") &&
                (source["officialBaseCommit"] as string) == "6ffaa7f0b0b7f3140367de96c8eacba533c96894";
            // Audited hardcap trees retain the ordinary ordered cohort entry (88a67 only; ban 7237).
            battlePassHistoryPatch = battlePassHistoryPatch || currentStatePatch;
            battlePassInspectionPatch = battlePassInspectionPatch || battlePassHistoryPatch;
            bool localPatch = inspectionPatch || rewardInspectionPatch || battlePassInspectionPatch || (record != null && source != null && (record["candidateKind"] as string) == "isolated-local-ww-source-patch" &&
                new[] { "20a2d40d0eaa0cfa053ceafecd9a4d04395291f4", "737c89ef00e36248baa0ab92df248af935528b56", "4a1b39d002c5754557bdc7b30782fad0048cd106", "567e71c93c32f03a7bd3ad013205438c0adcba11" }.Contains(source["commit"] as string) && source.ContainsKey("officialBaseCommit") &&
                (source["officialBaseCommit"] as string) == "f5f38c8d9e2b7f7e91edad77bd5fab14df3f162d");
            bool officialSource = record != null && source != null && (record["candidateKind"] as string) == "isolated-official-ww-source" &&
                new[] { "c3fef9a06f61f96abeb53571fb47dd578b6f7063", "07a578842b34dda3428535eb46f8b98f76fddb0d", "36c557437de745cdef506bcfa148569d3e09cfcf", "f5f38c8d9e2b7f7e91edad77bd5fab14df3f162d", "2243d465d58c74f52765b658174bff76165656ee" }.Contains(source["commit"] as string);
            if (!NumberEquals(record == null ? null : record["schemaVersion"], 1) || (record["status"] as string) != "read-only-import-ready" || !(officialSource || localPatch) || source == null || (source["repository"] as string) != "https://github.com/ok-oldking/ok-wuthering-waves.git" || (source["sourceDirectory"] as string) != "WW/source" || archive == null || (archive["file"] as string) != "official-source.zip" || !(archive["sha256"] is string) || entry == null || (entry["sourceEntry"] as string) != "main.py" || (entry["arguments"] as string) != "main.py --task 1 --exit" || !String.Equals(entry["pythonwRelativePath"] as string, "r\\Scripts\\pythonw.exe", StringComparison.OrdinalIgnoreCase) || !String.Equals(entry["workingDirectoryRelativePath"] as string, "WW/source", StringComparison.OrdinalIgnoreCase) || !File.Exists(expectedPythonw) || !File.Exists(expectedMain) || !File.Exists(expectedArchive) || !String.Equals(Hash(expectedArchive), archive["sha256"] as string, StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("configured_official_source_candidate_invalid");
            Binding binding = MakeBinding("WW", game, candidateRoot, expectedPythonw,
                "\"" + expectedMain + "\" --task 1 --exit", expectedDailyConfig, dailyTaskProfile, expectedWorking);
            binding.AppName = localPatch ? "ok-ww-local-source-patch-candidate" : "ok-ww-official-source-candidate";
            binding.WwInspectionSupported = inspectionPatch || rewardInspectionPatch || battlePassInspectionPatch;
            binding.WwRewardInspectionSupported = rewardInspectionPatch || battlePassInspectionPatch;
            binding.WwBattlePassInspectionSupported = battlePassInspectionPatch;
            binding.InnerPython = expectedPythonw;
            binding.InnerEntry = expectedMain;
            return binding;
        }
private static bool IsWwHardcapCohortSource(string working, IDictionary<string, object> record) {
            try {
                var source = record["source"] as IDictionary<string, object>;
                var templates = record["templates"] as IDictionary<string, object>;
            string commit = source == null ? null : source["commit"] as string;
            string dailyHash = Hash(Path.Combine(working, "src", "task", "DailyTask.py"));
            string sourceIndex = commit == "e28a46f93972c4fa1894f82f5fe788e7816fcd45" && dailyHash == "2a452cd3cbefa5347416e903e536019726449373c8ed260cebeac9b1f4691c17" ?
                "2ae25602430834af0ed93e8d9ca7731efa4524b68a039e120779f5dcbfa9aae9" :
                commit == "ca8dd9b96f1ecf0c5e02e61c9ef4a3ed60d9776a" && dailyHash == "51337ab585f5c56a1063d75544756f8a699afbbd553f4434f231cb80e2d062df" ?
                "11bd003986977de6099796a63b6379132d59b2637a546734b07fe48695383aad" :
                commit == "814eddb3e1acd05bc06f7fad212214e6d872a668" && dailyHash == "51337ab585f5c56a1063d75544756f8a699afbbd553f4434f231cb80e2d062df" ?
                "2688e7a8312fbccc20a07ec319631e1085763906d927de4580d13d9328aa6973" :
                commit == "6c5938976094c7423de808d7ce323df2cc39c2d3" && dailyHash == "51337ab585f5c56a1063d75544756f8a699afbbd553f4434f231cb80e2d062df" ?
                "60d11cd8fe15719fcacbd42729eb8c9cb180e187afb1c7be4847470b164d86f6" :
                commit == "20d2a0fbd5cbe257d80cc2de5f70d369ba25d07f" && dailyHash == "51337ab585f5c56a1063d75544756f8a699afbbd553f4434f231cb80e2d062df" ?
                "8269e9e4e0499ef895213998c9fe45319efc103e42b2a084ea7aeecca79b0aaa" :
                commit == "88a67e6a1719315954f5120aaf9d6a7819b28d78" && dailyHash == "b1966a153c49f718f42a157de81431ec10d2f9de16427b72974106fe987a3041" ?
                "351acc3af398c0e118b2e57649ea45343cd1901855ab0934c4b1fe2dcf7eca99" : null;
            if (sourceIndex == null) return false;
            int sourceCount = commit == "88a67e6a1719315954f5120aaf9d6a7819b28d78" ? 316 : (commit == "20d2a0fbd5cbe257d80cc2de5f70d369ba25d07f" ? 304 : 303);
                return source != null && templates != null &&
                    (!new[] { "ca8dd9b96f1ecf0c5e02e61c9ef4a3ed60d9776a", "814eddb3e1acd05bc06f7fad212214e6d872a668", "6c5938976094c7423de808d7ce323df2cc39c2d3", "20d2a0fbd5cbe257d80cc2de5f70d369ba25d07f", "88a67e6a1719315954f5120aaf9d6a7819b28d78" }.Contains(commit) ||
                        IsAuditedWwOcrCachePatch(Path.GetDirectoryName(Path.GetDirectoryName(working)))) &&
                    (templates["repository"] as string) == "https://github.com/ok-oldking/ok-wuthering-waves-coco-labeling.git" &&
                    (templates["commit"] as string) == "438526eafcf4b1caf78c1dea68ef36b42e81e3c4" &&
                    (templates["directory"] as string) == "WW/source/ok_templates" &&
                    AuditedWwBlobTree(working, source["blobs"] as object[], sourceCount,
                        sourceIndex, true) &&
                    AuditedWwBlobTree(Path.Combine(working, "ok_templates"), templates["blobs"] as object[], 165,
                        "ca1d7d95b077c5517d5b6d515d35200ccb42e043aa333a18fd5421fae358b001", false);
            } catch { return false; }
        }

private static bool AuditedWwBlobTree(string root, object[] rows, int count, string expectedDigest, bool source) {
            if (rows == null || rows.Length != count) return false;
            root = Path.GetFullPath(root);
            for (var parent = new DirectoryInfo(root); parent != null; parent = parent.Parent)
                if ((parent.Attributes & FileAttributes.ReparsePoint) != 0) return false;
            var files = new SortedDictionary<string, string>(StringComparer.Ordinal);
            var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var raw in rows) {
                var row = raw as IDictionary<string, object>;
                if (row == null || row.Count != 2 || !row.ContainsKey("path") || !row.ContainsKey("gitBlobSha1")) return false;
                string path = row["path"] as string, blob = row["gitBlobSha1"] as string;
                if (String.IsNullOrEmpty(path) || path.Any(Char.IsControl) || path.Contains('\\') || path.Contains(':') ||
                    Path.IsPathRooted(path) || path.Split('/').Any(p => p.Length == 0 || p == "." || p == "..") ||
                    !Regex.IsMatch(blob ?? "", "\\A[0-9a-f]{40}\\z") || !names.Add(path)) return false;
                files.Add(path, blob);
            }
            string index = String.Concat(files.Select(p => p.Key + "\0" + p.Value + "\n"));
            using (var digest = SHA256.Create())
                if (BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(index))).Replace("-", "").ToLowerInvariant() != expectedDigest) return false;
            if (!WwBlobInventory(root, source).SetEquals(names)) return false;
            var stamps = new List<Tuple<string, long, DateTime>>();
            long bytes = 0;
            foreach (var row in files) {
                string path = Path.Combine(root, row.Key.Replace('/', Path.DirectorySeparatorChar));
                for (var parent = new DirectoryInfo(Path.GetDirectoryName(path)); parent != null; parent = parent.Parent) {
                    if ((parent.Attributes & FileAttributes.ReparsePoint) != 0) return false;
                    if (String.Equals(parent.FullName, root, StringComparison.OrdinalIgnoreCase)) break;
                }
                var before = new FileInfo(path);
                if (!before.Exists || (before.Attributes & FileAttributes.ReparsePoint) != 0 || before.Length > 256L * 1024 * 1024 ||
                    (bytes += before.Length) > 1024L * 1024 * 1024) return false;
                long size = before.Length; DateTime modified = before.LastWriteTimeUtc;
                using (var hash = SHA1.Create()) using (var stream = File.OpenRead(path)) {
                    byte[] header = Encoding.ASCII.GetBytes("blob " + size.ToString(CultureInfo.InvariantCulture) + "\0");
                    hash.TransformBlock(header, 0, header.Length, header, 0);
                    byte[] buffer = new byte[65536]; int read;
                    while ((read = stream.Read(buffer, 0, buffer.Length)) != 0) hash.TransformBlock(buffer, 0, read, buffer, 0);
                    hash.TransformFinalBlock(new byte[0], 0, 0);
                    if (BitConverter.ToString(hash.Hash).Replace("-", "").ToLowerInvariant() != row.Value) return false;
                }
                var after = new FileInfo(path);
                if (after.Length != size || after.LastWriteTimeUtc != modified || (after.Attributes & FileAttributes.ReparsePoint) != 0) return false;
                stamps.Add(Tuple.Create(path, size, modified));
            }
            return WwBlobInventory(root, source).SetEquals(names) && stamps.All(stamp => {
                var file = new FileInfo(stamp.Item1);
                return file.Exists && file.Length == stamp.Item2 && file.LastWriteTimeUtc == stamp.Item3 &&
                    (file.Attributes & FileAttributes.ReparsePoint) == 0;
            });
        }

private static HashSet<string> WwBlobInventory(string root, bool source) {
            var result = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var pending = new Stack<string>(); pending.Push(root); int entries = 0;
            while (pending.Count != 0) {
                string directory = pending.Pop();
                foreach (string path in Directory.EnumerateFileSystemEntries(directory)) {
                    if (++entries > 20000) throw new InvalidDataException("ww_source_inventory_bound");
                    var attributes = File.GetAttributes(path);
                    if ((attributes & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("ww_source_reparse");
                    string relative = path.Substring(root.Length + 1).Replace('\\', '/');
                    // Runtime configuration/output is not source; templates have
                    // their own complete fixed tree, checked separately.
                    if (relative == ".git" || (source && new[] {"configs", "logs", "screenshots", "screenshot", "ok_templates"}.Contains(relative))) continue;
                    if ((attributes & FileAttributes.Directory) != 0) pending.Push(path);
                    else if (!result.Add(relative)) throw new InvalidDataException("ww_source_case_alias");
                }
            }
            return result;
        }

private static void AssertWwOcrPathNoLinks(string path) {
            for (string part = Path.GetFullPath(path); !String.IsNullOrEmpty(part); part = Path.GetDirectoryName(part)) {
                FileAttributes attributes;
                try { attributes = File.GetAttributes(part); }
                catch (FileNotFoundException) { continue; }
                catch (DirectoryNotFoundException) { continue; }
                if ((attributes & FileAttributes.ReparsePoint) != 0) throw new InvalidOperationException("ww_ocr_cache_redirected_path");
            }
        }

private static string GetWwOcrCacheDirectory(string root) {
            string path = Path.Combine(Path.GetFullPath(root), "mutable", "onnxocr");
            AssertWwOcrPathNoLinks(path);
            if (File.Exists(path) || File.Exists(Path.GetDirectoryName(path))) throw new InvalidOperationException("ww_ocr_cache_not_directory");
            return path;
        }

private static bool IsAuditedWwOcrCachePatch(string root) {
            try {
                string path = Path.Combine(root, "WW", "runtime-requalification-ocr-cache.json");
                string predict = Path.Combine(root, "r", "Lib", "site-packages", "onnxocr", "predict_base.py");
                string record = Path.Combine(root, "r", "Lib", "site-packages", "onnxocr_ppocrv5-0.0.22.dist-info", "RECORD");
                AssertWwOcrPathNoLinks(path); AssertWwOcrPathNoLinks(predict); AssertWwOcrPathNoLinks(record);
                GetWwOcrCacheDirectory(root);
                if (!File.Exists(path) || new FileInfo(path).Length > 128 * 1024) return false;
                var receipt = Json.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as IDictionary<string, object>;
                return receipt != null && NumberEquals(receipt["schemaVersion"], 1) &&
                    (receipt["status"] as string) == "runtime-files-requalified" &&
                    String.Equals(receipt["runtimeRoot"] as string, Path.GetFullPath(root), StringComparison.OrdinalIgnoreCase) &&
                    (receipt["parentManifestSha256"] as string) == Hash(Path.Combine(root, "WW", "runtime-preparation.json")) &&
                    (receipt["upstreamRepository"] as string) == "https://github.com/ok-oldking/OnnxOCR.git" &&
                    (receipt["officialBaseCommit"] as string) == "1888fd16e3ac3c663e2b25739cb21c388fb8a6fa" &&
                    (receipt["upstreamPatchCommit"] as string) == "9fd82161a575a994f70ef5a45e759b66dc7c7224" &&
                    (receipt["predictBaseSha256"] as string) == "cf80ceec7f6cb6c116811321a820c37aba012118deb29601bf99ddc61db2b2ec" &&
                    Hash(predict) == (receipt["predictBaseSha256"] as string) &&
                    (receipt["recordSha256"] as string) == "7b4d4e3c72f1ce5a90491408e92b0ce53f6298953f6d69800c82a25e5f4fb6c5" &&
                    Hash(record) == (receipt["recordSha256"] as string) &&
                    (receipt["cacheDirectoryRelativePath"] as string) == "mutable/onnxocr";
            } catch { return false; }
        }

        internal static bool IsAuditedWwCaptureSdkPatch(string root, IDictionary<string, object> record) {
            if (record == null || !record.ContainsKey("sdkSourcePatch")) return false;
            IDictionary<string, object> sdk = record["sdkSourcePatch"] as IDictionary<string, object>;
            if (sdk == null || new[] { "repository", "officialBaseCommit", "commit", "version", "candidateKind", "windowsGraphicsSha256", "wheelFile", "wheelSha256" }.Any(key => !sdk.ContainsKey(key))) return false;
            const string wheelName = "ok_script-2.0.8+local.wgcclose.20261007-py3-none-any.whl";
            string sourceFile = Path.Combine(root, "r", "Lib", "site-packages", "ok", "device", "capture_methods", "windows_graphics.py");
            string wheel = Path.Combine(root, "source-patches", wheelName);
            return (sdk["repository"] as string) == "https://github.com/ok-oldking/ok-script.git" &&
                (sdk["officialBaseCommit"] as string) == "752e01d02c9e298a3128f11d9ec0ed9c654dd0e6" &&
                (sdk["commit"] as string) == "0442e1f200bad209342c468f2ed44106c16cb9f6" &&
                (sdk["version"] as string) == "2.0.8+local.wgcclose.20261007" &&
                (sdk["candidateKind"] as string) == "local-upstream-sdk-source-patch" &&
                (sdk["windowsGraphicsSha256"] as string) == "d6f0d75442b0dc627a5bcfa7b7ae874083826b5a2047de88bc6b7296d4e9a047" &&
                (sdk["wheelFile"] as string) == "source-patches/" + wheelName &&
                (sdk["wheelSha256"] as string) == "bfb4a4f000d41df8ad8e4ed96144c2c8a2895ae6587f9161e4d55c34605ffe07" &&
                File.Exists(sourceFile) && File.Exists(wheel) &&
                Hash(sourceFile) == "d6f0d75442b0dc627a5bcfa7b7ae874083826b5a2047de88bc6b7296d4e9a047" &&
                Hash(wheel) == "bfb4a4f000d41df8ad8e4ed96144c2c8a2895ae6587f9161e4d55c34605ffe07";
        }
        // An isolated Endfield source candidate is deliberately a fixed layout,
        // rather than a generic manifest launcher.  The preparation record only
        // proves provenance and file integrity; none of its path or argv values
        // are used to construct the child process command.
        private static Binding TryMakeEndfieldOfficialSourceCandidateBinding(string game, string root, IDictionary<string, object> dailyTaskProfile) {
            if (String.IsNullOrWhiteSpace(root) || root.StartsWith("\\\\")) return null;
            string candidateRoot = Path.GetFullPath(root);
            string recordPath = Path.Combine(candidateRoot, "Endfield", "runtime-preparation.json");
            if (!File.Exists(recordPath)) return null;
            IDictionary<string, object> record = Json.DeserializeObject(File.ReadAllText(recordPath, Encoding.UTF8)) as IDictionary<string, object>;
            IDictionary<string, object> source = record == null ? null : record["source"] as IDictionary<string, object>;
            IDictionary<string, object> hashes = source == null ? null : source["keyFileSha256"] as IDictionary<string, object>;
            IDictionary<string, object> entry = record == null ? null : record["entry"] as IDictionary<string, object>;
            string expectedWorking = Path.Combine(candidateRoot, "Endfield", "source");
            string expectedPythonw = Path.Combine(candidateRoot, "r", "Scripts", "pythonw.exe");
            string expectedMain = Path.Combine(expectedWorking, "main.py");
            string expectedDailyConfig = Path.Combine(expectedWorking, "configs", "DailyTask.json");
            string gameFlow = Path.Combine(expectedWorking, "src", "core", "base_mixin", "game_flow_mixin.py");
            string creditCollect = Path.Combine(expectedWorking, "src", "tasks", "onetime", "CreditCollectTask.py");
            bool rewardInspectionPatch = EndfieldInspectionSourceMatches(candidateRoot, source);
            bool localPatch = record != null && source != null && hashes != null &&
                (record["candidateKind"] as string) == "isolated-local-source-patch-with-formal-runtime-binding" &&
                source.ContainsKey("officialBaseCommit") &&
                (((source["commit"] as string) == "ec5058f90b91f816201227cd66b75770422c7c67" &&
                  (source["officialBaseCommit"] as string) == "34626b32e5cf5f8a7039b0d10c898169a5c421d6") ||
                 ((source["commit"] as string) == "194b7c94f2e29980667c41a1b16df0556cacd6be" &&
                  (source["officialBaseCommit"] as string) == "bb5af7df07297fdb6edfc483cedd7167840d5e82") ||
                 ((source["commit"] as string) == "19eb6d2015850b25f6d27c74edf27bf932567503" &&
                  (source["officialBaseCommit"] as string) == "ef58c1c2d87e0f80a0075324e4797c1fbf267d5c") ||
                 ((source["commit"] as string) == "84e5df86197a68c4001f3a810b38c953aad60950" &&
                  (source["officialBaseCommit"] as string) == "ef58c1c2d87e0f80a0075324e4797c1fbf267d5c") ||
                 ((source["commit"] as string) == "402b2da9bc46be6a2876f3f1ec4ed732754b636f" &&
                  (source["officialBaseCommit"] as string) == "08af80be42ae11fdd482fbdb0b00f87f08cdf2e3") ||
                 ((source["commit"] as string) == "168d7470760d88b01f6441b1d3255b08f4316954" &&
                  (source["officialBaseCommit"] as string) == "cfa1069a30eab05f9bc26ff571cfa7222b9d064e" &&
                  File.Exists(creditCollect) && String.Equals(Hash(creditCollect), "fe4020ff763b6d292be1a4d063f42fc4d47d3e24b9971088102c0ba16d8674e8", StringComparison.OrdinalIgnoreCase)) ||
                 ((source["commit"] as string) == "b69a1c12c6907ed0830320ae7433111ddea5e675" &&
                  (source["officialBaseCommit"] as string) == "cfa1069a30eab05f9bc26ff571cfa7222b9d064e" &&
                  File.Exists(creditCollect) && String.Equals(Hash(creditCollect), "6dc3024a5fa7511bf5e69de4aebd47619ab2dbbe10452b9baccddc356f5f38b5", StringComparison.OrdinalIgnoreCase))) &&
                hashes.ContainsKey("gameFlow") && File.Exists(gameFlow) && String.Equals(Hash(gameFlow), hashes["gameFlow"] as string, StringComparison.OrdinalIgnoreCase);
            bool officialSource = record != null && source != null &&
                (record["candidateKind"] as string) == "isolated-official-source-with-formal-runtime-binding" &&
                ((source["commit"] as string) == "b13f1197a503e6473fbf63c1f927a33838739d1f" ||
                 (source["commit"] as string) == "9b45815dcefdb3d4c273fdc26482782eb1e18395" ||
                 (source["commit"] as string) == "0815bdf3da08bcf5de0b0e0d55efbd7b7719ace4" ||
                 (source["commit"] as string) == "60bb694e63658adeeec83cc474a5a06626f25c6e" ||
                 (source["commit"] as string) == "83434dc40e2db364eb9dfa0fe9d0234bc4d7a164" ||
                 (source["commit"] as string) == "3d1d521f6fb367c8da2cd480511585fd219e69aa");
            if (!NumberEquals(record == null ? null : record["schemaVersion"], 1) ||
                (record["status"] as string) != "read-only-import-ready" ||
                !(officialSource || localPatch || rewardInspectionPatch) ||
                source == null || (source["repository"] as string) != "https://github.com/AliceJump/ok-end-field.git" ||
                (source["sourceDirectory"] as string) != "Endfield/source" || hashes == null ||
                !(hashes["main"] is string) || entry == null ||
                (entry["sourceEntry"] as string) != "main.py" || (entry["arguments"] as string) != "main.py --task 1 --exit" ||
                !String.Equals(entry["pythonw"] as string, expectedPythonw, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(entry["workingDirectory"] as string, expectedWorking, StringComparison.OrdinalIgnoreCase) ||
                !File.Exists(expectedPythonw) || !File.Exists(expectedMain) || !String.Equals(Hash(expectedMain), hashes["main"] as string, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("configured_official_source_candidate_invalid");
            Binding binding = MakeBinding("Endfield", game, candidateRoot, expectedPythonw,
                "\"" + expectedMain + "\" --task 1 --exit", expectedDailyConfig, dailyTaskProfile, expectedWorking);
            binding.AppName = localPatch || rewardInspectionPatch ? "ok-ef-local-source-patch-candidate" : "ok-ef-official-source-candidate";
            binding.EndfieldRewardInspectionSupported = rewardInspectionPatch;
            binding.InnerPython = expectedPythonw;
            binding.InnerEntry = expectedMain;
            return binding;
        }
        // GF2 candidates have the same constrained source layout as Endfield:
        // the manifest is evidence to validate, never a caller-controlled
        // command template.  Keep the fixed main.py DailyTask entry below.
        private static Binding TryMakeGf2OfficialSourceCandidateBinding(string game, string root, IDictionary<string, object> dailyTaskProfile) {
            if (String.IsNullOrWhiteSpace(root) || root.StartsWith("\\\\")) return null;
            string candidateRoot = Path.GetFullPath(root);
            string recordPath = Path.Combine(candidateRoot, "GF2", "runtime-preparation.json");
            if (!File.Exists(recordPath)) return null;
            IDictionary<string, object> record = Json.DeserializeObject(File.ReadAllText(recordPath, Encoding.UTF8)) as IDictionary<string, object>;
            IDictionary<string, object> source = record == null ? null : record["source"] as IDictionary<string, object>;
            IDictionary<string, object> hashes = source == null ? null : source["keyFileSha256"] as IDictionary<string, object>;
            IDictionary<string, object> entry = record == null ? null : record["entry"] as IDictionary<string, object>;
            string expectedWorking = Path.Combine(candidateRoot, "GF2", "source");
            string expectedPythonw = Path.Combine(candidateRoot, "r", "Scripts", "pythonw.exe");
            string expectedMain = Path.Combine(expectedWorking, "main.py");
            string expectedDailyConfig = Path.Combine(expectedWorking, "configs", "DailyTask.json");
            bool officialRelease1277 = source != null &&
                (source["repository"] as string) == "https://github.com/AliceJump/ok-gf2.git" &&
                (source["commit"] as string) == "5f19a91bed0c98d3cbd919e40cbfcfe0e34b3f42" &&
                source.ContainsKey("tag") && (source["tag"] as string) == "v1.2.77";
            bool approvedCandidate = ApprovedGf2SourceMatches(record, source, expectedWorking);
            if (!NumberEquals(record == null ? null : record["schemaVersion"], 1) ||
                (record["status"] as string) != "read-only-import-ready" ||
                ((record["candidateKind"] as string) != "isolated-official-gf2-source" && !approvedCandidate) ||
                source == null || (!(officialRelease1277 || approvedCandidate) && ((source["repository"] as string) != "https://cnb.cool/ok-oldking/ok-gf2.git" ||
                ((source["commit"] as string) != "4122e01e8a69063ae37807f316f2ae8f80eeedb1" &&
                 (source["commit"] as string) != "13a569429b9b0b50db06e95b0cb1f65bf54a38b7"))) ||
                (source["sourceDirectory"] as string) != "GF2/source" || hashes == null ||
                !(hashes["main"] is string) || entry == null ||
                (entry["sourceEntry"] as string) != "main.py" || (entry["arguments"] as string) != "main.py --task 1 --exit" ||
                !String.Equals(entry["pythonw"] as string, expectedPythonw, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(entry["workingDirectory"] as string, expectedWorking, StringComparison.OrdinalIgnoreCase) ||
                !File.Exists(expectedPythonw) || !File.Exists(expectedMain) || !String.Equals(Hash(expectedMain), hashes["main"] as string, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("configured_official_source_candidate_invalid");
            if (record.ContainsKey("sdkSourcePatch")) {
                IDictionary<string, object> sdk = record["sdkSourcePatch"] as IDictionary<string, object>;
                string sdkFile = Path.Combine(candidateRoot, "r", "Lib", "site-packages", "ok", "device", "interaction_methods", "genshin.py");
                const string sdkHash = "a23a384e038dfb012ef7d4dbba76d0bdbaf4e13b993efdaef9a6a2d74cbb5cd8";
                if (!officialRelease1277 || sdk == null ||
                    SdkSourceField(sdk, "repository") != "https://github.com/ok-oldking/ok-script.git" ||
                    SdkSourceField(sdk, "officialBaseCommit") != "752e01d02c9e298a3128f11d9ec0ed9c654dd0e6" ||
                    SdkSourceField(sdk, "commit") != "dabd9f7f99c2dec7ce1ff90a6f84576e0dc99ca4" ||
                    SdkSourceField(sdk, "version") != "2.0.8+local.genshincleanup.20261003" ||
                    SdkSourceField(sdk, "candidateKind") != "local-upstream-sdk-source-patch" ||
                    SdkSourceField(sdk, "genshinSha256") != sdkHash || !File.Exists(sdkFile) ||
                    !String.Equals(Hash(sdkFile), sdkHash, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("configured_official_sdk_candidate_invalid");
            }
            Binding binding = MakeBinding("GF2", game, candidateRoot, expectedPythonw,
                "\"" + expectedMain + "\" --task 1 --exit", expectedDailyConfig, dailyTaskProfile, expectedWorking);
            binding.AppName = "ok-gf2-official-source-candidate";
            binding.Gf2SupplyRewardChoiceSupported = officialRelease1277 || approvedCandidate;
            binding.Gf2RewardInspectionSupported = approvedCandidate && source.ContainsKey("inspectionVersion") &&
                (source["inspectionVersion"] as string) == "gf2.reward-status.inspect@1.0";
            binding.InnerPython = expectedPythonw;
            binding.InnerEntry = expectedMain;
            return binding;
        }
        private static bool ApprovedGf2SourceMatches(IDictionary<string, object> record, IDictionary<string, object> source, string working) {
            if (record == null || source == null) return false;
            string registryPath = Path.Combine(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location), "gf2-source-candidates.json");
            if (!File.Exists(registryPath)) return false;
            var registry = Json.DeserializeObject(File.ReadAllText(registryPath, Encoding.UTF8)) as IDictionary<string, object>;
            if (registry == null || !NumberEquals(registry["schemaVersion"], 1)) return false;
            foreach (var row in (object[])registry["candidates"]) {
                var candidate = row as IDictionary<string, object>;
                if (candidate == null || SdkSourceField(candidate, "candidateKind") != SdkSourceField(record, "candidateKind")) continue;
                bool matches = true;
                foreach (string key in new[] { "repository", "commit", "tag", "officialBaseCommit" }) {
                    object actual;
                    source.TryGetValue(key, out actual);
                    if (candidate.ContainsKey(key) && !Object.Equals(candidate[key], actual)) matches = false;
                }
                if (source.ContainsKey("inspectionVersion") != candidate.ContainsKey("inspectionVersion") ||
                    (source.ContainsKey("inspectionVersion") && !Object.Equals(source["inspectionVersion"], candidate["inspectionVersion"]))) matches = false;
                var files = candidate["files"] as IDictionary<string, object>;
                if (!matches || files == null) continue;
                foreach (var file in files) {
                    string path = Path.GetFullPath(Path.Combine(working, file.Key.Replace('/', Path.DirectorySeparatorChar)));
                    if (!path.StartsWith(working.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase) ||
                        !File.Exists(path) || !String.Equals(Hash(path), file.Value as string, StringComparison.OrdinalIgnoreCase)) matches = false;
                }
                if (matches) return true;
            }
            return false;
        }
        private static string SdkSourceField(IDictionary<string, object> fields, string name) {
            object value;
            return fields.TryGetValue(name, out value) ? value as string : null;
        }
private static void NormalizeGf2Profile(string updateState) {
            IDictionary<string, object> state = Json.DeserializeObject(File.ReadAllText(updateState, Encoding.UTF8)) as IDictionary<string, object>;
            if (state == null || !state.ContainsKey("profiles"))
                throw new InvalidOperationException("formal_gui_profile_state_invalid");
            IEnumerable profiles = state["profiles"] as IEnumerable;
            if (profiles == null) throw new InvalidOperationException("formal_gui_profile_state_invalid");
            List<string> names = new List<string>();
            foreach (object value in profiles) {
                IDictionary<string, object> profile = value as IDictionary<string, object>;
                if (profile != null && profile.ContainsKey("name") && profile["name"] is string && !String.IsNullOrWhiteSpace((string)profile["name"]))
                    names.Add((string)profile["name"]);
            }
            if (names.Count == 0) throw new InvalidOperationException("formal_gui_profile_state_invalid");
            string current = state.ContainsKey("current_profile") ? state["current_profile"] as string : null;
            if (!names.Contains(current, StringComparer.OrdinalIgnoreCase))
                state["current_profile"] = names.FirstOrDefault(delegate(string name) { return String.Equals(name, "china", StringComparison.OrdinalIgnoreCase); }) ?? names[0];
            // Older migrated PyAppify state omits this field even though the
            // installed version is already the version the formal entry will
            // start.  The updater contract requires the equality explicitly.
            if (!state.ContainsKey("app_starting_version") && state["current_version"] is string)
                state["app_starting_version"] = state["current_version"];
            string temporary = updateState + ".yeyu.tmp";
            File.WriteAllText(temporary, Json.Serialize(state), new UTF8Encoding(false));
            File.Replace(temporary, updateState, null);
        }
        private static Binding MakeBinding(string gameId, string game, string toolRoot, string executable, string arguments, string dailyConfig, IDictionary<string, object> dailyTaskProfile = null, string workingDirectory = null) {
            string effectiveWorkingDirectory = String.IsNullOrEmpty(workingDirectory) ? toolRoot : workingDirectory;
            if (String.IsNullOrEmpty(game) || String.IsNullOrEmpty(toolRoot) || !File.Exists(game) || !File.Exists(executable) ||
                String.IsNullOrEmpty(Path.GetDirectoryName(dailyConfig)) || !Directory.Exists(effectiveWorkingDirectory)) throw new InvalidOperationException("configured_installation_missing");
            Binding result = new Binding { GameId = gameId, Game = game, ToolRoot = toolRoot, WorkingDirectory = effectiveWorkingDirectory, Executable = executable, Arguments = arguments, DailyConfig = dailyConfig, DailyTaskProfile = dailyTaskProfile };
            return result;
        }
        private static bool NumberEquals(object value, int expected) { return (value is int && (int)value == expected) || (value is decimal && (decimal)value == expected); }
        private static int RunDaily(Binding binding, string[] selectedOperations, Action<string, string, string> onStage, Func<bool> cancellationRequested, bool observationReplay, string observationFile, out string detail, out bool cancelled) {
            var groups = WwInspectionGroups(binding, selectedOperations);
            // Manager deliberately skips client launch for shared-observation
            // replay. Preserve that no-process contract; missing verification
            // remains unresolved for a separately managed live inspection.
            if (observationReplay || groups.Count <= 1)
                return RunDailySingle(binding, selectedOperations, onStage, cancellationRequested, observationReplay, observationFile, out detail, out cancelled);
            bool progress = binding.WwInspectionOnly, reward = binding.WwRewardInspectionOnly, pass = binding.WwBattlePassInspectionOnly;
            detail = String.Empty;
            cancelled = false;
            try {
                for (int index = 0; index < groups.Count; index++) {
                    if (cancellationRequested()) { cancelled = true; detail = "ww_readonly_inspection_cancelled_before_next_entry"; return 0; }
                    string[] group = groups[index];
                    binding.WwInspectionOnly = group.Contains("inspect-daily-progress");
                    binding.WwRewardInspectionOnly = group.Contains("claim-daily-reward");
                    binding.WwBattlePassInspectionOnly = group.Contains("claim-battle-pass");
                    bool first = index == 0;
                    Action<string, string, string> observe = (operation, state, message) => {
                        // Session entry is one Todo, although the official
                        // saved-account task checks it for each invocation.
                        if (first || operation != "attach-world") onStage(operation, state, message);
                    };
                    int exit = RunDailySingle(binding, group, observe, cancellationRequested, false, String.Empty, out detail, out cancelled);
                    if (exit != 0 || cancelled) return exit;
                }
                return 0;
            } finally {
                binding.WwInspectionOnly = progress;
                binding.WwRewardInspectionOnly = reward;
                binding.WwBattlePassInspectionOnly = pass;
            }
        }
        private static int RunDailySingle(Binding binding, string[] selectedOperations, Action<string, string, string> onStage, Func<bool> cancellationRequested, bool observationReplay, string observationFile, out string detail, out bool cancelled) {
            // A duplicated case-variant environment name ("Path" beside "PATH")
            // makes every ProcessStartInfo.EnvironmentVariables read throw, which
            // would stop this runner before it can start the official tool.
            // Collapse such names before the start info below is populated.
            YeYuGamer.AdapterHost.UpstreamConsoleCapture.NormalizeInheritedEnvironment();
            detail = String.Empty;
            cancelled = false;
            byte[] originalDailyConfig = null;
            bool originalDailyConfigExisted = false;
            bool dailyConfigSnapshotTaken = false;
            var splitConfigSnapshots = new List<ConfigSnapshot>();
            Process process = null;
            string processArguments = binding.Arguments;
            if (binding.GameId == "WW" && binding.AccountEnabled && processArguments.IndexOf(" --task 1 ", StringComparison.Ordinal) >= 0)
                processArguments = processArguments.Replace(" --task 1 ", " --task 7 ");
            if (observationReplay) {
                ReplayWwOfficialObservations(binding, observationFile, onStage);
                detail = "ww_official_multi_account_replay";
                return 0;
            }
            var start = new ProcessStartInfo { FileName = binding.Executable, Arguments = processArguments, WorkingDirectory = binding.WorkingDirectory, UseShellExecute = false, CreateNoWindow = true };
            string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            string stageFile = Path.Combine(staging, binding.GameId.ToLowerInvariant() + "-stage-" + Guid.NewGuid().ToString("N") + ".jsonl");
            File.WriteAllText(stageFile, String.Empty, new UTF8Encoding(false));
            var seenStageLines = new HashSet<string>(StringComparer.Ordinal);
            bool humanRequired = false;
            Action<string, string, string> observeStage = (operation, state, stageDetail) => {
                onStage(operation, state, stageDetail);
                if (state == "human_required" && selectedOperations.Contains(operation)) humanRequired = true;
            };
            try {
            RestorePendingDailyConfig(binding);
            originalDailyConfigExisted = File.Exists(binding.DailyConfig);
            originalDailyConfig = originalDailyConfigExisted ? File.ReadAllBytes(binding.DailyConfig) : null;
            dailyConfigSnapshotTaken = true;
            splitConfigSnapshots = binding.EndfieldRewardInspectionOnly ? new List<ConfigSnapshot>() : SnapshotEndfieldSplitConfig(binding);
            EnsureDailyConfigForInjection(binding.DailyConfig);
            if (binding.Gf2RewardInspectionOnly) {
                var inspectionConfig = new Dictionary<string, object> {
                    { "委托状态", selectedOperations.Contains("claim-daily-missions") },
                    { "巡录状态", selectedOperations.Contains("battle-pass-free-track") }, { "Exit After Task", true }
                };
                File.WriteAllText(binding.DailyConfig, Json.Serialize(inspectionConfig), new UTF8Encoding(false));
            } else if (binding.EndfieldRewardInspectionOnly) {
                var inspectionConfig = Json.DeserializeObject(File.ReadAllText(binding.DailyConfig, Encoding.UTF8)) as IDictionary<string, object>;
                if (inspectionConfig == null) throw new InvalidDataException("endfield_inspection_config_invalid");
                // The official daily reward operation includes both handbook
                // and pass rewards. Never confirm it from a daily-only page.
                inspectionConfig["查看通行证"] = true;
                File.WriteAllText(binding.DailyConfig, Json.Serialize(inspectionConfig), new UTF8Encoding(false));
            } else {
            if (binding.GameId == "WW" && binding.DailyTaskProfile != null) ApplyOkWWDailyTaskProfile(binding);
            if (binding.GameId == "Endfield" && binding.DailyTaskProfile != null) ApplyEndfieldDailyTaskProfile(binding);
            if (binding.GameId == "GF2" && binding.DailyTaskProfile != null) ApplyGf2DailyTaskProfile(binding);
            ApplySelectedOfficialDailyConfig(binding, selectedOperations);
            }
            start.EnvironmentVariables["YEYU_GAMER_STAGE_FILE"] = stageFile;
            start.EnvironmentVariables["YEYU_GAMER_SELECTED_OPERATIONS"] = Json.Serialize(selectedOperations);
            start.EnvironmentVariables["YEYU_GAMER_GAME_ID"] = binding.GameId;
            start.EnvironmentVariables["YEYU_GAMER_GF2_REWARD_INSPECTION_ONLY"] = binding.Gf2RewardInspectionOnly ? "1" : "0";
            start.EnvironmentVariables["YEYU_GAMER_ENDFIELD_REWARD_INSPECTION_ONLY"] = binding.EndfieldRewardInspectionOnly ? "1" : "0";
            start.EnvironmentVariables["YEYU_GAMER_UPSTREAM_LOG_FILE"] = Path.Combine(staging, binding.GameId.ToLowerInvariant() + "-upstream-" + Guid.NewGuid().ToString("N") + ".jsonl");
            start.EnvironmentVariables["PYTHONPATH"] = AppDomain.CurrentDomain.BaseDirectory + ";" + binding.WorkingDirectory;
            start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
            ConfigureToolOutput(start);
                process = Process.Start(start);
                if (process == null) throw new InvalidOperationException("tool_start_failed");
                DrainToolOutput(process, Path.Combine(staging, binding.GameId.ToLowerInvariant() + "-upstream-console-" + Guid.NewGuid().ToString("N") + ".jsonl"));
                // Each fixed entry starts the upstream Qt GUI and selects its
                // first one-time DailyTask. The GUI remains visible while its
                // own updater/startup path runs and emits finer-grained stages.
                onStage("official-tool-start", "tool_started", binding.GameId + " official process started; pid=" + process.Id + "; waiting for its DailyTask runtime");
                while (!process.WaitForExit(150)) {
                    process.Refresh();
                    if (cancellationRequested != null && cancellationRequested()) {
                        // This stops only the fixed tool child. The game client is
                        // intentionally not part of the process tree we terminate.
                        StopToolProcess(process);
                        detail = "cancelled_by_manager";
                        cancelled = true;
                        return process.HasExited ? process.ExitCode : -1;
                    }
                    DrainOkWWStageEvents(stageFile, seenStageLines, observeStage);
                    if (humanRequired) {
                        // Stop only the fixed tool child; retain the game client
                        // and the official unresolved choice for the user.
                        StopToolProcess(process);
                        detail = "official_human_choice_required; fixed tool stopped; game client retained";
                        cancelled = false;
                        return 30;
                    }
                }
                process.WaitForExit(); // Complete asynchronous stdout/stderr callbacks before sealing logs.
                DrainOkWWStageEvents(stageFile, seenStageLines, observeStage);
                if (humanRequired) { detail = "official_human_choice_required; official tool exited"; return 30; }
                int toolExitCode = SafeExitCode(process);
                detail = "fixedTask=daily; gameId=" + binding.GameId + "; toolExitCode=" + toolExitCode +
                    "; official process exited"; return toolExitCode;
            } finally {
                bool stopped = process == null;
                if (process != null) {
                    try { StopToolProcess(process); stopped = process.HasExited; if (stopped) process.WaitForExit(); }
                    catch (Exception error) { detail = (detail ?? String.Empty) + ";toolStopFailed=" + error.GetType().Name; }
                }
                if (dailyConfigSnapshotTaken) {
                    try { RestoreEndfieldSplitConfig(splitConfigSnapshots, stopped); }
                    finally {
                    if (stopped) RestoreOrPreserveDailyConfig(binding.DailyConfig, originalDailyConfig, originalDailyConfigExisted);
                    else {
                        // Preserve the original until the bound tool really
                        // exits; a live child can otherwise overwrite restore.
                        PreserveDailyConfigForRecovery(binding.DailyConfig, originalDailyConfig, originalDailyConfigExisted);
                        throw new InvalidOperationException("tool_stop_incomplete: original daily config retained for recovery");
                    }
                    }
                }
                if (process != null) process.Dispose();
            }
        }
        private static void RestorePendingDailyConfig(Binding binding) {
            string pending = binding.DailyConfig + ".yeyu-restore-pending";
            string legacyAbsent = pending + ".absent";
            bool hasPending = File.Exists(pending);
            bool hasLegacyAbsent = File.Exists(legacyAbsent);
            if (!hasPending && !hasLegacyAbsent) return;
            if (hasPending && hasLegacyAbsent) throw new InvalidOperationException("daily_config_restore_pending_invalid");
            string expected = Path.GetFullPath(binding.Executable);
            foreach (Process candidate in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(expected))) {
                using (candidate) {
                    if (candidate.HasExited) continue;
                    string image;
                    try { image = Path.GetFullPath(candidate.MainModule.FileName); }
                    catch { throw new InvalidOperationException("tool_stop_unverified: pending daily config recovery"); }
                    if (String.Equals(image, expected, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidOperationException("tool_still_running: pending daily config recovery");
                }
            }
            // Compatibility with the short-lived two-file absent marker.  New
            // pending writes use only the self-describing record below.
            if (hasLegacyAbsent) {
                RestoreDailyConfigSnapshot(binding.DailyConfig, null, false);
                File.Delete(legacyAbsent);
                return;
            }
            byte[] record = File.ReadAllBytes(pending);
            bool hasHeader = record.Length >= PendingDailyConfigHeader.Length + 7 &&
                PendingDailyConfigHeader.SequenceEqual(record.Take(PendingDailyConfigHeader.Length));
            if (!hasHeader) {
                // Compatibility with the previous raw-byte recovery format.
                RestoreDailyConfigSnapshot(binding.DailyConfig, record, true);
                File.Delete(pending);
                return;
            }
            int markerStart = PendingDailyConfigHeader.Length;
            string marker = Encoding.ASCII.GetString(record, markerStart, 7);
            if (marker == "ABSENT\n") RestoreDailyConfigSnapshot(binding.DailyConfig, null, false);
            else if (marker == "PRESENT") {
                if (record.Length < markerStart + 8 || record[markerStart + 7] != (byte)'\n')
                    throw new InvalidOperationException("daily_config_restore_pending_invalid");
                RestoreDailyConfigSnapshot(binding.DailyConfig, record.Skip(markerStart + 8).ToArray(), true);
            } else throw new InvalidOperationException("daily_config_restore_pending_invalid");
            // Delete only after the requested restoration. If either action
            // fails, the pending record remains and the next run retries.
            File.Delete(pending);
        }
        private static readonly byte[] PendingDailyConfigHeader = Encoding.ASCII.GetBytes("YEYU-OPENKURO-RESTORE-V1\n");
        // Missing DailyTask.json is a supported upstream state: Config loads
        // its class defaults and persists them.  The empty document below is
        // only this run's carrier for selected official overrides; upstream
        // still owns all omitted default values.
        private static void EnsureDailyConfigForInjection(string path) {
            if (File.Exists(path)) return;
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            File.WriteAllText(path, "{}", new UTF8Encoding(false));
        }
        private static void RestoreDailyConfigSnapshot(string path, byte[] content, bool existed) {
            if (existed) {
                if (content == null) throw new InvalidOperationException("daily_config_snapshot_invalid");
                File.WriteAllBytes(path, content);
            } else if (File.Exists(path)) File.Delete(path);
        }
        private static void RestoreOrPreserveDailyConfig(string path, byte[] content, bool existed) {
            try { RestoreDailyConfigSnapshot(path, content, existed); }
            catch (Exception restoreError) {
                try { PreserveDailyConfigForRecovery(path, content, existed); }
                catch (Exception pendingError) {
                    throw new InvalidOperationException("daily_config_restore_failed_and_recovery_pending_failed",
                        new AggregateException(restoreError, pendingError));
                }
                // The original restoration error remains visible to the
                // caller, but its source bytes are now durable for retry.
                throw;
            }
        }
        private static void PreserveDailyConfigForRecovery(string path, byte[] content, bool existed) {
            string pending = path + ".yeyu-restore-pending";
            if (File.Exists(pending)) return;
            if (existed && content == null) throw new InvalidOperationException("daily_config_snapshot_invalid");
            byte[] marker = Encoding.ASCII.GetBytes(existed ? "PRESENT\n" : "ABSENT\n");
            byte[] payload = content ?? new byte[0];
            byte[] record = new byte[PendingDailyConfigHeader.Length + marker.Length + payload.Length];
            Buffer.BlockCopy(PendingDailyConfigHeader, 0, record, 0, PendingDailyConfigHeader.Length);
            Buffer.BlockCopy(marker, 0, record, PendingDailyConfigHeader.Length, marker.Length);
            Buffer.BlockCopy(payload, 0, record, PendingDailyConfigHeader.Length + marker.Length, payload.Length);
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            string temporary = pending + ".tmp." + Guid.NewGuid().ToString("N");
            try {
                File.WriteAllBytes(temporary, record);
                try { File.Move(temporary, pending); }
                catch (IOException) { if (!File.Exists(pending)) throw; }
            } finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }
        private static void StopStaleFormalProcesses(Binding binding) {
            foreach (string executable in new string[] { binding.Executable, binding.InnerPython }) {
                if (String.IsNullOrEmpty(executable)) continue;
                foreach (Process process in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(executable))) {
                    try {
                        string image = null;
                        try { image = process.MainModule.FileName; }
                        catch (System.ComponentModel.Win32Exception) { }
                        catch (InvalidOperationException) { }
                        bool isFormalExecutable = String.Equals(Path.GetFullPath(executable), Path.GetFullPath(binding.Executable), StringComparison.OrdinalIgnoreCase);
                        bool owned = !String.IsNullOrEmpty(image) && (isFormalExecutable
                            ? String.Equals(Path.GetFullPath(image), Path.GetFullPath(executable), StringComparison.OrdinalIgnoreCase)
                            : Path.GetFullPath(image).StartsWith(Path.GetFullPath(binding.ToolRoot).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase));
                        if (owned) {
                            StopToolProcess(process);
                        } else process.Dispose();
                    } catch (InvalidOperationException) { process.Dispose(); }
                }
            }
        }
        private static void ConfigureToolOutput(ProcessStartInfo start) {
            YeYuGamer.AdapterHost.UpstreamConsoleCapture.Configure(start);
        }
        private static void DrainToolOutput(Process process, string logPath) {
            YeYuGamer.AdapterHost.UpstreamConsoleCapture.Drain(process, logPath);
        }
        private static void StopToolProcess(Process process) {
            if (process == null || process.HasExited) return;
            try { process.CloseMainWindow(); } catch (InvalidOperationException) { }
            if (!process.WaitForExit(3000)) {
                try { process.Kill(); } catch (InvalidOperationException) { }
                process.WaitForExit(5000);
            }
        }
        private static int SafeExitCode(Process process) {
            if (process == null) return -1;
            try { return process.ExitCode; }
            catch (InvalidOperationException) {
                // Elevated GUI children discovered through Process enumeration
                // can be waited successfully while Windows withholds the exit
                // code from this non-parent handle. Stage events remain the
                // semantic completion authority for the fixed daily task.
                try { return process.HasExited ? 0 : -1; }
                catch (InvalidOperationException) { return -1; }
            }
        }
        private static void DrainOkWWStageEvents(string stageFile, HashSet<string> seenLines, Action<string, string, string> onStage) {
            if (String.IsNullOrEmpty(stageFile) || onStage == null || !File.Exists(stageFile)) return;
            string[] lines;
            try { lines = ReadSharedLines(stageFile); }
            catch (IOException) { return; }
            foreach (string line in lines) {
                if (String.IsNullOrWhiteSpace(line) || !seenLines.Add(line)) continue;
                IDictionary<string, object> record;
                try { record = Json.DeserializeObject(line) as IDictionary<string, object>; }
                catch (Exception) { continue; }
                if (record == null) continue;
                string operation = record.ContainsKey("operation") ? record["operation"] as string : null;
                string state = record.ContainsKey("state") ? record["state"] as string : null;
                string detail = record.ContainsKey("detail") ? record["detail"] as string : String.Empty;
                if (!String.IsNullOrEmpty(operation) && !String.IsNullOrEmpty(state)) {
                    string account = record.ContainsKey("account") ? record["account"] as string : null;
                    if (!String.IsNullOrEmpty(account))
                        detail = "account=" + account + (String.IsNullOrEmpty(detail) ? String.Empty : "; " + detail);
                    onStage(operation, state, detail ?? String.Empty);

                }
            }
        }

        private static string NormalizeWwAccount(string account) {
            if (String.IsNullOrEmpty(account)) return String.Empty;
            return account.ToLowerInvariant().Replace('0', 'o').Replace(".con", ".com");
        }
        private static bool WwAccountsMatch(string left, string right) {
            string a = NormalizeWwAccount(left);
            string b = NormalizeWwAccount(right);
            return !String.IsNullOrEmpty(a) && a == b;
        }
        private static void WriteWwInspectionScope(Binding binding, IDictionary<string, object> request,
            string todoId, string todoAttemptId, string imagePath, string observedAt) {
            WriteWwVerificationScope(binding, request, todoId, todoAttemptId, imagePath, observedAt, "inspect-daily-progress");
        }
        private static void WriteWwVerificationScope(Binding binding, IDictionary<string, object> request,
            string todoId, string todoAttemptId, string imagePath, string observedAt, string operation) {
            bool reward = operation == "claim-daily-reward";
            bool battlePass = operation == "claim-battle-pass";
            bool allowed = (operation == "inspect-daily-progress" && binding.WwInspectionSupported && binding.WwInspectionOnly)
                || (reward && binding.WwRewardInspectionSupported && binding.WwRewardInspectionOnly)
                || (battlePass && binding.WwBattlePassInspectionSupported && binding.WwBattlePassInspectionOnly);
            allowed |= binding.WwInspectionOperations != null && binding.WwInspectionOperations.Contains(operation)
                && ((operation == "inspect-daily-progress" && binding.WwInspectionSupported)
                    || (reward && binding.WwRewardInspectionSupported) || (battlePass && binding.WwBattlePassInspectionSupported));
            if (!allowed || !binding.AccountEnabled)
                throw new InvalidDataException("ww_inspection_identity_unconfirmed");
            string imageStem = Path.GetFileNameWithoutExtension(imagePath);
            if (!Regex.IsMatch(imageStem, "^ww-recognition-[0-9a-f]{32}_original$"))
                throw new InvalidDataException("ww_inspection_source_name_invalid");
            // OfficialStepCapture uses <recognition-id>_original.png and
            // <recognition-id>.json, not <recognition-id>_original.json.
            string sourceName = imageStem.Substring(0, imageStem.Length - "_original".Length) + ".json";
            string sourcePath = Path.Combine(Path.GetDirectoryName(imagePath), sourceName);
            var recognition = Json.DeserializeObject(File.ReadAllText(sourcePath, Encoding.UTF8)) as IDictionary<string, object>;
            if (recognition == null || (recognition["operation"] as string) != operation ||
                (recognition["recognitionType"] as string) != (reward ? "template" : "ocr") || (recognition["coordinateSpace"] as string) != "full-frame" ||
                (reward && (recognition["template"] as string) != "daily_reward_claimed_check") ||
                (battlePass && (!recognition.ContainsKey("verificationPage") || !new[] { "tasks", "rewards" }.Contains(recognition["verificationPage"] as string))) ||
                !WwAccountsMatch(recognition["upstreamConfirmedAccount"] as string, binding.SavedAccountLabel) ||
                (recognition["observedAt"] as string) != observedAt || (recognition["screenshotSha256"] as string) != Hash(imagePath))
                throw new InvalidDataException("ww_inspection_source_mismatch");
            var scope = new Dictionary<string, object> {
                {"schemaVersion", 1}, {"gameId", "WW"}, {"accountId", binding.AccountId},
                {"runId", request["runId"]}, {"runAttemptId", request["runAttemptId"]},
                {"todoInstanceId", todoId}, {"todoAttemptId", todoAttemptId},
                {"observedAt", observedAt}, {"sourceMetadataFile", sourceName},
                {"sourceMetadataSha256", Hash(sourcePath)}, {"sha256", Hash(imagePath)},
                {"kind", reward || battlePass ? "game-ui-reward-verification-raw" : "game-ui-key-step-verification-raw"},
                {"boundary", battlePass ? "DailyTask.inspect_battle_pass_page" : reward ? "DailyTask.inspect_daily_reward_status" : "DailyTask.inspect_daily_progress"}
            };
            if (battlePass) scope["verificationPage"] = recognition["verificationPage"];
            File.WriteAllText(imagePath + ".recognition.json", Json.Serialize(scope), new UTF8Encoding(false));
        }
        private static void AppendWwObservation(string path, string operation, string state, string detail, string account) {
            if (String.IsNullOrEmpty(path) || String.IsNullOrEmpty(operation) || String.IsNullOrEmpty(state)) return;
            try {
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                if (state == "step_artifact") {
                    string fileName = DetailValue(detail, "file");
                    string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
                    if (String.IsNullOrEmpty(fileName) || fileName != Path.GetFileName(fileName) || String.IsNullOrEmpty(staging)) return;
                    string source = Path.Combine(staging, fileName);
                    string destination = Path.Combine(Path.GetDirectoryName(path), fileName);
                    if (!String.Equals(Path.GetFullPath(source), Path.GetFullPath(destination), StringComparison.OrdinalIgnoreCase))
                        File.Copy(source, destination, true);
                }
                var record = new Dictionary<string, object> {
                    { "operation", operation }, { "state", state }, { "detail", detail ?? String.Empty },
                    { "at", DateTime.Now.ToString("o") }
                };
                if (!String.IsNullOrEmpty(account)) record["account"] = account;
                File.AppendAllText(path, Json.Serialize(record) + "\n", new UTF8Encoding(false));
            } catch (Exception) { }
        }
        private static void ImportWwSidecarArtifact(string observationFile, string operation, string state, string detail, Dictionary<string, List<Tuple<string, string, string>>> evidence) {
            string fileName = DetailValue(detail, "file");
            if (String.IsNullOrEmpty(observationFile) || String.IsNullOrEmpty(fileName) || fileName != Path.GetFileName(fileName)) return;
            string sourceDir = Path.GetDirectoryName(observationFile);
            string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            if (String.IsNullOrEmpty(sourceDir) || String.IsNullOrEmpty(staging)) return;
            string source = Path.Combine(sourceDir, fileName);
            string destination = Path.Combine(staging, fileName);
            if (!File.Exists(source)) return;
            try {
                if (!String.Equals(Path.GetFullPath(source), Path.GetFullPath(destination), StringComparison.OrdinalIgnoreCase))
                    File.Copy(source, destination, true);
            } catch (Exception) { return; }
            string kind = state == "step_artifact" ? DetailValue(detail, "kind") : state == "sidecar_before" ? "game-ui-step-before-raw"
                : state == "sidecar_after_watermarked" ? "game-ui-step-after-watermarked"
                : "game-ui-step-after-raw";
            string capturedAt = DetailValue(detail, "capturedAt");
            if (String.IsNullOrEmpty(capturedAt) || (!IsStepArtifactKind(kind) &&
                !(operation == "inspect-daily-progress" && kind == "game-ui-key-step-verification-raw") &&
                !((operation == "claim-daily-reward" || operation == "claim-battle-pass") && kind == "game-ui-reward-verification-raw"))) return;
            List<Tuple<string, string, string>> items;
            if (!evidence.TryGetValue(operation, out items)) {
                items = new List<Tuple<string, string, string>>();
                evidence[operation] = items;
            }
            if (!items.Any(item => item.Item1 == fileName)) items.Add(Tuple.Create(fileName, kind, capturedAt));
        }
        private static bool IsStepArtifactKind(string kind) {
            // Old artifacts keep their original semantics during replay.
            // Recognition inputs are process evidence, never automatically
            // renamed to dedicated reward/key-step verification frames.
            return new[] { "game-ui-step-before-raw", "game-ui-step-after-raw", "game-ui-step-after-watermarked",
                "game-ui-daily-reward-before", "game-ui-daily-reward-raw", "game-ui-daily-reward-watermarked",
                "game-ui-recognition-raw", "game-ui-recognition-replay-raw", "official-recognition-metadata" }.Contains(kind);
        }
        private static Tuple<string, string, string> ParseStepArtifactDescriptor(string gameId, bool gf2InspectionOnly, string detail) {
            string fileName = DetailValue(detail, "file");
            string kind = DetailValue(detail, "kind");
            bool inspectionMetadata = gameId == "GF2" && gf2InspectionOnly && kind == "game-ui-recognition-meta";
            if (String.IsNullOrEmpty(fileName) || Path.GetFileName(fileName) != fileName ||
                (!IsStepArtifactKind(kind) && !inspectionMetadata)) return null;
            return Tuple.Create(fileName, kind, inspectionMetadata ? "text/plain" : StepArtifactMimeType(kind));
        }
        private static string StepArtifactMimeType(string kind) {
            return kind == "official-recognition-metadata" ? "text/plain" : "image/png";
        }
        private static void ReplayWwOfficialObservations(Binding binding, string observationFile, Action<string, string, string> onStage) {
            if (String.IsNullOrEmpty(observationFile) || !File.Exists(observationFile) || onStage == null) return;
            string[] lines;
            try { lines = ReadSharedLines(observationFile); }
            catch (IOException) { return; }
            foreach (string line in lines) {
                IDictionary<string, object> record;
                try { record = Json.DeserializeObject(line) as IDictionary<string, object>; }
                catch (Exception) { continue; }
                if (record == null) continue;
                string operation = record.ContainsKey("operation") ? record["operation"] as string : null;
                string state = record.ContainsKey("state") ? record["state"] as string : null;
                string detail = record.ContainsKey("detail") ? record["detail"] as string : String.Empty;
                string account = record.ContainsKey("account") ? record["account"] as string : null;
                if (String.IsNullOrEmpty(operation) || String.IsNullOrEmpty(state)) continue;
                if (state == "capture_failed") continue;
                if (!WwAccountsMatch(account, binding.SavedAccountLabel)) continue;
                if (!String.IsNullOrEmpty(account))
                    detail = "account=" + account + (String.IsNullOrEmpty(detail) ? String.Empty : "; " + detail);
                // A replayed observation re-presents the shared writer run's
                // physical evidence inside this attempt's own lease. The
                // writer's capture instant predates this lease, so a verbatim
                // replay is rejected by the Manager as
                // artifact_time_out_of_scope. The protocol time describes this
                // new sealing, while the original observation stays in detail
                // and the immutable metadata file. Recognition images receive
                // an explicit replay kind; historical action labels remain.
                detail = RestampReplayedCaptureTimes(detail);
                onStage(operation, state, detail ?? String.Empty);
            }
        }
        private static string RestampReplayedCaptureTimes(string detail) {
            if (String.IsNullOrEmpty(detail)) return detail;
            string kind = DetailValue(detail, "kind");
            if (kind == "game-ui-recognition-raw" || kind == "game-ui-recognition-replay-raw" ||
                kind == "official-recognition-metadata") {
                string originalObservedAt = DetailValue(detail, "originalObservedAt");
                if (String.IsNullOrEmpty(originalObservedAt)) {
                    originalObservedAt = DetailValue(detail, "capturedAt");
                    if (!String.IsNullOrEmpty(originalObservedAt)) detail += "; originalObservedAt=" + originalObservedAt;
                }
                if (kind == "game-ui-recognition-raw")
                    detail = ReplaceDetailValue(detail, "kind", "game-ui-recognition-replay-raw");
            }
            string now = DateTime.Now.ToString("o");
            // `capturedAt` is a shorter, lower-case sibling of
            // `launchTimeoutCapturedAt`; the ordinal search keeps them apart.
            foreach (string key in new[] { "capturedAt", "launchTimeoutCapturedAt" })
                detail = ReplaceDetailValue(detail, key, now);
            return detail;
        }
        private static string ReplaceDetailValue(string detail, string key, string value) {
            if (String.IsNullOrEmpty(detail) || String.IsNullOrEmpty(key)) return detail;
            string marker = key + "=";
            int search = 0;
            while (true) {
                int start = detail.IndexOf(marker, search, StringComparison.Ordinal);
                if (start < 0) return detail;
                start += marker.Length;
                int end = detail.IndexOf(';', start);
                detail = end < 0
                    ? detail.Substring(0, start) + value
                    : detail.Substring(0, start) + value + detail.Substring(end);
                search = start + value.Length;
            }
        }
        private static string[] ReadSharedLines(string path) {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            using (var reader = new StreamReader(stream, Encoding.UTF8, true))
                return reader.ReadToEnd().Split(new string[] { "\r\n", "\n" }, StringSplitOptions.RemoveEmptyEntries);
        }
        private static string DetailValue(string detail, string key) {
            if (String.IsNullOrEmpty(detail) || String.IsNullOrEmpty(key)) return String.Empty;
            string marker = key + "=";
            int start = detail.IndexOf(marker, StringComparison.Ordinal);
            if (start < 0) return String.Empty;
            start += marker.Length;
            int end = detail.IndexOf(';', start);
            string value = end < 0 ? detail.Substring(start) : detail.Substring(start, end - start);
            return value.Trim();
        }
        private static string TypedFailureReason(string detail, string fallback) {
            foreach (string code in new[] { "launcher_download_required", "client_update_required", "telemetry_missing" })
                if (!String.IsNullOrEmpty(detail) && detail.StartsWith(code, StringComparison.Ordinal)) return code;
            return fallback;
        }
        private static void ApplySelectedOfficialDailyConfig(Binding binding, string[] selectedOperations) {
            if (binding.GameId == "WW") {
                if (!binding.WwInspectionSupported) return;
                var wwConfig = Json.DeserializeObject(File.ReadAllText(binding.DailyConfig, Encoding.UTF8)) as IDictionary<string, object>;
                if (wwConfig == null) throw new InvalidOperationException("daily_config_invalid");
                wwConfig["Daily Progress Inspection Only"] = binding.WwInspectionOnly;
                if (binding.WwRewardInspectionSupported)
                    wwConfig["Daily Reward Status Inspection Only"] = binding.WwRewardInspectionOnly;
                if (binding.WwBattlePassInspectionSupported)
                    wwConfig["Battle Pass Status Inspection Only"] = binding.WwBattlePassInspectionOnly;
                wwConfig["Daily Progress Inspection Account"] = (binding.WwInspectionOnly || binding.WwRewardInspectionOnly || binding.WwBattlePassInspectionOnly) ? binding.SavedAccountLabel : String.Empty;
                File.WriteAllText(binding.DailyConfig, Json.Serialize(wwConfig), new UTF8Encoding(false));
                return;
            }
            var selected = new HashSet<string>(selectedOperations ?? new string[0], StringComparer.Ordinal);
            IDictionary<string, object> config = Json.DeserializeObject(File.ReadAllText(binding.DailyConfig, Encoding.UTF8)) as IDictionary<string, object>;
            if (config == null) throw new InvalidOperationException("daily_config_invalid");
            if (binding.GameId == "Endfield") {
                // The official plan defaults several omitted switches to true.
                // A scoped seven-operation run must explicitly disable its
                // other plan entries; omission would execute unselected work.
                // Original bytes and all task parameters are restored afterward.
                foreach (string key in new[] {
                    "⭐送礼", "⭐帝江号整理", "⭐帝江号一键存放", "⭐简易制作",
                    "⭐自动送货", "⭐造装备", "⭐买信用商店", "⭐演算",
                    "⭐传送到帝江号右侧传送点", "⭐执行外部命令"
                }) config[key] = false;
                config["⭐地区建设"] = new string[0];
                config["⭐活动奖励"] = new string[0];
                config["⭐收邮件"] = selected.Contains("mail");
                config["⭐刷体力"] = selected.Contains("spend-sanity");
                config["⭐转交运送委托"] = selected.Contains("delivery-commission");
                config["⭐收信用"] = selected.Contains("collect-credit");
                // This official multi-selection is account-configured. Preserve
                // its current list when selected instead of substituting a
                // YeYu-defined list; an unselected operation is the only case
                // that injects the official empty-list disablement.
                if (!selected.Contains("dijiang-harvest")) config["⭐帝江号收菜"] = new string[0];
                config["⭐日常奖励"] = selected.Contains("claim-daily-reward");
                config["Exit After Task"] = false;
                ApplyEndfieldTaskGroups(binding, selected, config);
                ApplyEndfieldSplitConfig(binding, selected, config);
            } else if (binding.GameId == "GF2") {
                config["邮件"] = selected.Contains("mail");
                config["情报和战前补给"] = selected.Contains("activity-supply");
                config["闪耀星愿"] = selected.Contains("star-wish");
                config["活动自律"] = selected.Contains("event-battle");
                config["探索领取"] = selected.Contains("exploration-claim");
                // Legacy GF2 DailyTask combines activity work under this old
                // switch. It must not inherit a saved true value when only
                // the compatible seven-operation scope is launched.
                if (!IsGf2MainSourceBinding(binding)) config["情报补给"] = false;
                config["活动层"] = false;
                config["竞技场"] = false;
                config["购买免费礼包"] = false;
                config["商店心愿单购买"] = false;
                config["公共区/调度室"] = selected.Contains("public-area-dispatch");
                config["自动刷体力"] = selected.Contains("spend-stamina");
                config["班组"] = selected.Contains("squad-tasks");
                config["领任务"] = selected.Contains("claim-daily-missions");
                config["大月卡"] = selected.Contains("battle-pass-free-track");
                config["Exit After Task"] = false;
            } else return;
            string temporary = binding.DailyConfig + ".yeyu." + Guid.NewGuid().ToString("N") + ".tmp";
            try { File.WriteAllText(temporary, Json.Serialize(config), new UTF8Encoding(false)); ReplaceConfigFile(temporary, binding.DailyConfig); }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }

        private static void ApplyOkWWDailyTaskProfile(Binding binding) {
            IDictionary<string, object> profile = binding.DailyTaskProfile;
            var required = new HashSet<string>(StringComparer.Ordinal) {
                "whichToFarm", "tacetSuppressionNumber", "forgeryChallengeNumber", "materialSelection",
                "farmNightmareNestForDailyEcho", "additionalTasks"
            };
            if (profile == null || profile.Count != required.Count || profile.Keys.Any(key => !required.Contains(key))) throw new InvalidOperationException("ok_ww_daily_profile_invalid");
            string target = profile["whichToFarm"] as string;
            string material = profile["materialSelection"] as string;
            int tacet = PositiveInt(profile["tacetSuppressionNumber"]);
            int forgery = PositiveInt(profile["forgeryChallengeNumber"]);
            var allowedAdditional = new HashSet<string>(StringComparer.Ordinal) {
                "Check Weekly Garden", "Auto Farm all Nightmare Nest",
                "Merge Echo If discarded > 1000", "Teleport and Farm 4C Echo"
            };
            IEnumerable rawAdditional = profile["additionalTasks"] as IEnumerable;
            if (rawAdditional == null || profile["additionalTasks"] is string) throw new InvalidOperationException("ok_ww_daily_profile_invalid");
            var additional = new List<string>();
            foreach (object value in rawAdditional) {
                string item = value as string;
                if (item == null || !allowedAdditional.Contains(item) || additional.Contains(item)) throw new InvalidOperationException("ok_ww_daily_profile_invalid");
                additional.Add(item);
            }
            if ((target != "Tacet Suppression" && target != "Forgery Challenge" && target != "Simulation Challenge") ||
                (material != "Resonator EXP" && material != "Weapon EXP" && material != "Shell Credit") ||
                !(profile["farmNightmareNestForDailyEcho"] is bool)) throw new InvalidOperationException("ok_ww_daily_profile_invalid");
            IDictionary<string, object> config = Json.DeserializeObject(File.ReadAllText(binding.DailyConfig, Encoding.UTF8)) as IDictionary<string, object>;
            if (config == null) throw new InvalidOperationException("ok_ww_daily_config_invalid");
            config["Which to Farm"] = target;
            config["Which Tacet Suppression to Farm"] = tacet;
            config["Which Forgery Challenge to Farm"] = forgery;
            config["Material Selection"] = material;
            config["Farm Nightmare Nest for Daily Echo"] = (bool)profile["farmNightmareNestForDailyEcho"];
            config["Additional Tasks to Run After Daily Task"] = additional;
            config["Exit After Task"] = false;
            string temporary = binding.DailyConfig + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try {
                File.WriteAllText(temporary, Json.Serialize(config), new UTF8Encoding(false));
                ReplaceConfigFile(temporary, binding.DailyConfig);
            } finally {
                if (File.Exists(temporary)) File.Delete(temporary);
            }
        }
        // These values are the published OK-EF DailyBattleFeature keys.  This
        // adapter only supplies a temporary config document; deciding today's
        // rotation entry and performing the battle remains upstream.
        private static void ApplyEndfieldDailyTaskProfile(Binding binding) {
            IDictionary<string, object> profile = binding.DailyTaskProfile;
            var required = new HashSet<string>(StringComparer.Ordinal) {
                "staminaStage", "rewardTier", "staminaRotationStartDate", "staminaRotation", "teamSlot"
            };
            if (profile == null || profile.Count != required.Count || profile.Keys.Any(key => !required.Contains(key)))
                throw new InvalidOperationException("endfield_daily_profile_invalid");
            string staminaStage = profile["staminaStage"] as string;
            string rewardTier = profile["rewardTier"] as string;
            string rotationStartDate = profile["staminaRotationStartDate"] as string;
            string teamSlot = profile["teamSlot"] as string;
            DateTime parsedRotationStartDate;
            if (!EndfieldStages.Contains(staminaStage) ||
                (rewardTier != "保持当前" && rewardTier != "低阶" && rewardTier != "高阶") ||
                !EndfieldTeamSlots.Contains(teamSlot) ||
                String.IsNullOrWhiteSpace(rotationStartDate) ||
                !DateTime.TryParseExact(rotationStartDate, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out parsedRotationStartDate))
                throw new InvalidOperationException("endfield_daily_profile_invalid");
            IEnumerable rawRotation = profile["staminaRotation"] as IEnumerable;
            if (rawRotation == null || profile["staminaRotation"] is string)
                throw new InvalidOperationException("endfield_daily_profile_invalid");
            var rotation = new List<string>();
            foreach (object value in rawRotation) {
                string stage = value as string;
                if (!IsEndfieldRotationStage(stage)) throw new InvalidOperationException("endfield_daily_profile_invalid");
                rotation.Add(stage);
                if (rotation.Count > 31) throw new InvalidOperationException("endfield_daily_profile_invalid");
            }
            string profilePath = IsEndfieldSplitConfig(binding) ? Path.Combine(Path.GetDirectoryName(binding.DailyConfig), "DailyBattleTask.json") : binding.DailyConfig;
            EnsureDailyConfigForInjection(profilePath);
            IDictionary<string, object> config = Json.DeserializeObject(File.ReadAllText(profilePath, Encoding.UTF8)) as IDictionary<string, object>;
            if (config == null) throw new InvalidOperationException("endfield_daily_config_invalid");
            config["体力本"] = staminaStage;
            config["体力本奖励档位"] = rewardTier;
            config["刷体力开始日期"] = rotationStartDate;
            config["刷本序列"] = rotation.ToArray();
            config["指定的队伍编号"] = teamSlot;
            string temporary = profilePath + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try {
                File.WriteAllText(temporary, Json.Serialize(config), new UTF8Encoding(false));
                ReplaceConfigFile(temporary, profilePath);
            } finally {
                if (File.Exists(temporary)) File.Delete(temporary);
            }
        }
        // GF2 keeps these values in its official DailyTask document.  Every
        // profile member is optional: an omitted or null member deliberately
        // leaves the user's official saved value untouched.
        private static void ApplyGf2DailyTaskProfile(Binding binding) {
            IDictionary<string, object> profile = binding.DailyTaskProfile;
            var allowed = new HashSet<string>(StringComparer.Ordinal) {
                "autoBattleConfirmed", "staminaStage", "extraCreditBattle", "autonomousLoop",
                "squadDust", "eventStageName", "drinkDurations", "mealDuration", "supplyRewardChoice"
            };
            if (profile == null || profile.Keys.Any(key => !allowed.Contains(key)))
                throw new InvalidOperationException("gf2_daily_profile_invalid");

            object value;
            if (profile.TryGetValue("autoBattleConfirmed", out value) && value != null && !(value is bool))
                throw new InvalidOperationException("gf2_daily_profile_invalid");
            if (profile.TryGetValue("extraCreditBattle", out value) && value != null && !(value is bool))
                throw new InvalidOperationException("gf2_daily_profile_invalid");
            if (profile.TryGetValue("autonomousLoop", out value) && value != null && !(value is bool))
                throw new InvalidOperationException("gf2_daily_profile_invalid");
            if (profile.TryGetValue("squadDust", out value) && value != null && !(value is bool))
                throw new InvalidOperationException("gf2_daily_profile_invalid");
            if (profile.TryGetValue("staminaStage", out value) && value != null &&
                (!(value is string) || !Gf2StaminaStages.Contains((string)value)))
                throw new InvalidOperationException("gf2_daily_profile_invalid");
            ValidateGf2Text(profile, "eventStageName", 128);
            ValidateGf2Text(profile, "drinkDurations", 64);
            ValidateGf2Text(profile, "mealDuration", 64);
            if (profile.TryGetValue("supplyRewardChoice", out value) && value != null) {
                if (!(value is string) || !Gf2SupplyRewardChoices.Contains((string)value))
                    throw new InvalidOperationException("gf2_daily_profile_invalid");
                if (!binding.Gf2SupplyRewardChoiceSupported)
                    throw new InvalidOperationException("gf2_supply_reward_choice_unsupported");
            }

            IDictionary<string, object> config = Json.DeserializeObject(File.ReadAllText(binding.DailyConfig, Encoding.UTF8)) as IDictionary<string, object>;
            if (config == null) throw new InvalidOperationException("gf2_daily_config_invalid");
            ApplyGf2ConfigValue(profile, config, "autoBattleConfirmed", "已确认启用游戏内全局自动功能");
            ApplyGf2ConfigValue(profile, config, "staminaStage", "体力本");
            ApplyGf2ConfigValue(profile, config, "extraCreditBattle", "刷钱本");
            ApplyGf2ConfigValue(profile, config, "autonomousLoop", "自主循环");
            ApplyGf2ConfigValue(profile, config, "squadDust", "尘烟");
            ApplyGf2ConfigValue(profile, config, "eventStageName", "当前物资关卡名称");
            ApplyGf2ConfigValue(profile, config, "drinkDurations", "喝水");
            ApplyGf2ConfigValue(profile, config, "mealDuration", "吃饭");
            ApplyGf2ConfigValue(profile, config, "supplyRewardChoice", "拂晓之光补给包奖励");
            string temporary = binding.DailyConfig + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try {
                File.WriteAllText(temporary, Json.Serialize(config), new UTF8Encoding(false));
                ReplaceConfigFile(temporary, binding.DailyConfig);
            } finally {
                if (File.Exists(temporary)) File.Delete(temporary);
            }
        }
        private static void ValidateGf2Text(IDictionary<string, object> profile, string key, int maximumLength) {
            object value;
            if (profile.TryGetValue(key, out value) && value != null && (!(value is string) || ((string)value).Length > maximumLength))
                throw new InvalidOperationException("gf2_daily_profile_invalid");
        }
        private static void ApplyGf2ConfigValue(IDictionary<string, object> profile, IDictionary<string, object> config, string profileKey, string configKey) {
            object value;
            if (profile.TryGetValue(profileKey, out value) && value != null) config[configKey] = value;
        }
        private static readonly HashSet<string> Gf2SupplyRewardChoices = new HashSet<string>(StringComparer.Ordinal) {
            "", "数据链路", "坍塌晶条", "增域存量条T2", "大容量内存条", "转录导体·序三", "萨狄斯金"
        };
        private static readonly HashSet<string> Gf2StaminaStages = new HashSet<string>(StringComparer.Ordinal) {
            "军备解析", "深度搜索", "决策构象", "定向"
        };
        private static readonly HashSet<string> EndfieldStages = new HashSet<string>(StringComparer.Ordinal) {
            "干员经验", "干员进阶", "钱币收集", "技能提升", "武器经验", "武器进阶", "罗丹", "三位一体", "白垩界卫", "阮一", "聂菲斯",
            "D96钢", "超距辉映管", "快子遴捡晶格", "象限拟合液", "三相纳米片", "枢纽区", "源石研究园", "试验园区", "矿脉源区", "供能高地", "武陵城", "清波寨", "首墩", "藏剑谷"
        };
        private static readonly HashSet<string> EndfieldTieredStages = new HashSet<string>(StringComparer.Ordinal) {
            "干员经验", "干员进阶", "技能提升", "武器进阶"
        };
        private static readonly HashSet<string> EndfieldTeamSlots = new HashSet<string>(StringComparer.Ordinal) {
            "不换队伍", "1", "2", "3", "4", "5"
        };
        private static bool IsEndfieldRotationStage(string value) {
            if (String.IsNullOrWhiteSpace(value)) return false;
            if (EndfieldStages.Contains(value)) return true;
            foreach (string tier in new[] { "低阶", "高阶" }) {
                if (value.EndsWith(tier, StringComparison.Ordinal))
                    return EndfieldTieredStages.Contains(value.Substring(0, value.Length - tier.Length));
            }
            return false;
        }
        private static int PositiveInt(object value) {
            int number;
            if (value is int) number = (int)value;
            else if (value is decimal && decimal.Truncate((decimal)value) == (decimal)value && (decimal)value <= Int32.MaxValue) number = (int)(decimal)value;
            else throw new InvalidOperationException("ok_ww_daily_profile_invalid");
            if (number < 1) throw new InvalidOperationException("ok_ww_daily_profile_invalid");
            return number;
        }
        private static int AttemptNumber(IDictionary<string, object> todo) {
            object value = todo == null || !todo.ContainsKey("priorAttempts") ? null : todo["priorAttempts"];
            if (value is int && (int)value >= 0) return checked((int)value + 1);
            if (value is decimal && decimal.Truncate((decimal)value) == (decimal)value && (decimal)value >= 0 && (decimal)value < Int32.MaxValue) return checked((int)(decimal)value + 1);
            throw new InvalidOperationException("prior_attempts_invalid");
        }
        private static Dictionary<string, object> Base(IDictionary<string, object> request, string gameId, string type, int sequence, Dictionary<string, object> extra) { var value = new Dictionary<string, object> { {"schemaVersion",1},{"protocolVersion",ProtocolVersion},{"eventType",type},{"sequence",sequence},{"runId",request["runId"]},{"runAttemptId",request["runAttemptId"]},{"fencingToken",request["fencingToken"]},{"gameId",gameId},{"at",DateTime.UtcNow.ToString("o")} }; foreach(var item in extra)value[item.Key]=item.Value; return value; }
        private static void Emit(Dictionary<string, object> value) { ProtocolOutput.WriteLine(Json.Serialize(value)); }
        private static string Hash(string path) { using(var stream=File.OpenRead(path)) using(var sha=SHA256.Create()) { return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-",String.Empty).ToLowerInvariant(); } }
        private static bool Probe() { try { LoadBinding("WW"); LoadBinding("Endfield"); LoadBinding("GF2"); return true; } catch { return false; } }
        private static string PackageVersion() { try { var d=Json.DeserializeObject(File.ReadAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"install-manifest.json"))) as IDictionary<string,object>; return d["packageVersion"] as string; } catch { return "unknown"; } }
        private static string PackageDigest() { try { string root=AppDomain.CurrentDomain.BaseDirectory; byte[] raw=File.ReadAllBytes(Path.Combine(root,"install-manifest.json")); var document=Json.DeserializeObject(Encoding.UTF8.GetString(raw)) as IDictionary<string,object>; object[] files=document["files"] as object[]; var entries=new SortedDictionary<string,string>(StringComparer.Ordinal); foreach(object item in files){var file=item as IDictionary<string,object>;entries.Add(file["path"] as string,file["sha256"] as string);} using(var sha=SHA256.Create()) using(var stream=new MemoryStream()){stream.Write(raw,0,raw.Length);foreach(var entry in entries){byte[] p=Encoding.UTF8.GetBytes(entry.Key);byte[] h=Encoding.ASCII.GetBytes(entry.Value);stream.Write(p,0,p.Length);stream.Write(h,0,h.Length);}return "sha256:"+BitConverter.ToString(sha.ComputeHash(stream.ToArray())).Replace("-",String.Empty).ToLowerInvariant();} } catch(Exception error) { throw new InvalidDataException("package_digest_unavailable", error); } }
        private static string TerminalDigest(IDictionary<string, object> request, string status, string[] attempted, string[] completed, string[] unresolved, int exitCode) { string payload=(request["runId"] as string)+"\n"+(request["runAttemptId"] as string)+"\n"+status+"\n"+String.Join("\n",attempted)+"\n--completed--\n"+String.Join("\n",completed)+"\n--unresolved--\n"+String.Join("\n",unresolved)+"\n"+exitCode; using(var sha=SHA256.Create()){return "sha256:"+BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(payload))).Replace("-",String.Empty).ToLowerInvariant();} }
        private static int TerminalSetupFailure(IDictionary<string, object> request, object[] ids, string reason) { string gameId=request["gameId"] as string; string[] unresolved=ids.Cast<string>().ToArray(); Emit(Base(request,gameId,"hello",0,new Dictionary<string,object>{{"packageId",PackageId},{"packageVersion",PackageVersion()},{"packageDigest",PackageDigest()},{"runnerPid",Process.GetCurrentProcess().Id},{"acceptedTodoInstanceIds",ids}})); Emit(Base(request,gameId,"run_terminal",1,new Dictionary<string,object>{{"status","review_required"},{"transportOutcome","clean"},{"attemptedTodoInstanceIds",new string[0]},{"completedTodoInstanceIds",new string[0]},{"unresolvedTodoInstanceIds",ids},{"terminalEventDigest",TerminalDigest(request,"review_required",new string[0],new string[0],unresolved,0)},{"exitCode",0}})); return 0; }
    }
}
