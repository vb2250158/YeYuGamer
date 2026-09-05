using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;

namespace YeYuGamer.ClassicSelectedDailyAdapter
{
    internal static class Program
    {
        private const string PackageId = "legacy-night-rain-gamer";
        private const string ProtocolVersion = "1.1";
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        private sealed class Binding
        {
            public string GameId;
            public string ToolRoot;
            public string GamePath;
            public string Python;
        }

        private sealed class Todo
        {
            public string InstanceId;
            public string AttemptId;
            public string Operation;
            public int AttemptNo;
            public bool Started;
            public bool Terminal;
            public bool Completed;
            public string Status;
        }

        public static int Main(string[] args)
        {
            Console.InputEncoding = new UTF8Encoding(false);
            Console.OutputEncoding = new UTF8Encoding(false);
            if (args != null && args.Length == 1 && args[0] == "--probe-binding")
            {
                bool ok = Probe();
                Console.Out.WriteLine(Json.Serialize(new Dictionary<string, object> {
                    { "ok", ok }, { "processStarted", false },
                    { "gameIds", new [] { "PGR", "ZZZ", "NIKKE" } }
                }));
                Console.Out.Flush();
                return ok ? 0 : 65;
            }
            Dictionary<string, string> options = ParseArguments(args);
            if (options == null || options["--protocol-version"] != ProtocolVersion || !SupportedGame(options["--game-id"])) return 64;
            IDictionary<string, object> request = Json.DeserializeObject(Console.In.ReadLine() ?? String.Empty) as IDictionary<string, object>;
            if (!MatchesRequest(request, options)) return 64;
            object[] ids = request["executableTodoInstanceIds"] as object[];
            object[] rawTodos = request["todos"] as object[];
            if (ids == null || rawTodos == null || ids.Length == 0 || ids.Length != rawTodos.Length) return 64;

            Binding binding;
            try { binding = LoadBinding(options["--game-id"]); }
            catch (Exception error) { return SetupFailure(request, ids, error.Message); }
            var todos = new List<Todo>();
            var seenOperations = new HashSet<string>(StringComparer.Ordinal);
            for (int index = 0; index < ids.Length; index++)
            {
                string instanceId = ids[index] as string;
                IDictionary<string, object> document = rawTodos[index] as IDictionary<string, object>;
                string operation = document == null ? null : document["operation"] as string;
                if (String.IsNullOrEmpty(instanceId) || String.IsNullOrEmpty(operation) || !AllowedOperation(binding.GameId, operation) || !seenOperations.Add(operation)) return 64;
                todos.Add(new Todo { InstanceId = instanceId, AttemptId = Guid.NewGuid().ToString(), Operation = operation, AttemptNo = AttemptNumber(document) });
            }

            string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            if (String.IsNullOrEmpty(staging) || !Directory.Exists(staging)) return 65;
            Emit(Base(request, binding.GameId, "hello", 0, new Dictionary<string, object> {
                { "packageId", PackageId }, { "packageVersion", PackageVersion() }, { "packageDigest", PackageDigest() },
                { "runnerPid", Process.GetCurrentProcess().Id }, { "acceptedTodoInstanceIds", ids }
            }));
            int sequence = 1;
            string stageFile = Path.Combine(staging, "classic-stage-" + Guid.NewGuid().ToString("N") + ".jsonl");
            File.WriteAllText(stageFile, String.Empty, new UTF8Encoding(false));
            string detail;
            int exitCode;
            try { exitCode = RunDriver(binding, todos.Select(item => item.Operation).ToArray(), stageFile, todos, request, ref sequence, out detail); }
            catch (Exception error) { exitCode = -1; detail = "classic_driver_start_failed: " + error.Message; }
            DrainStages(stageFile, todos, request, binding.GameId, ref sequence);
            foreach (Todo todo in todos)
            {
                if (todo.Started && !todo.Terminal) Terminal(todo, exitCode == 0 ? "review_required" : "failed", exitCode == 0 ? "driver_stage_event_missing" : "driver_failed", detail + "; stage_event_missing", request, binding.GameId, ref sequence);
            }
            bool allCompleted = exitCode == 0 && todos.All(item => item.Completed);
            bool anyHumanRequired = todos.Any(item => item.Status == "human_required");
            string status = allCompleted ? "completed" : (anyHumanRequired ? "human_required" : (exitCode == 0 ? "review_required" : "failed"));
            string[] attemptedIds = todos.Where(item => item.Started).Select(item => item.InstanceId).ToArray();
            string[] completedIds = todos.Where(item => item.Completed).Select(item => item.InstanceId).ToArray();
            string[] unresolvedIds = todos.Where(item => !item.Completed).Select(item => item.InstanceId).ToArray();
            Emit(Base(request, binding.GameId, "run_terminal", sequence++, new Dictionary<string, object> {
                { "status", status }, { "transportOutcome", exitCode == 0 ? "clean" : "crashed" },
                { "attemptedTodoInstanceIds", attemptedIds },
                { "completedTodoInstanceIds", completedIds },
                { "unresolvedTodoInstanceIds", unresolvedIds },
                { "terminalEventDigest", TerminalDigest(request, status, attemptedIds, completedIds, unresolvedIds, exitCode) }, { "exitCode", exitCode }
            }));
            // Adapter Host requires the process exit code to match the exitCode
            // declared by run_terminal.  Returning zero here hid real upstream
            // failures behind process_exit_mismatch and discarded the precise
            // Todo failure already emitted above.
            return exitCode;
        }

        private static int RunDriver(Binding binding, string[] operations, string stageFile, List<Todo> todos, IDictionary<string, object> request, ref int sequence, out string detail)
        {
            string driver = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "classic_tool_driver.py");
            if (!File.Exists(driver)) throw new FileNotFoundException("classic_driver_missing", driver);
            var output = new StringBuilder();
            var start = new ProcessStartInfo {
                FileName = binding.Python,
                Arguments = Quote(driver) + " --game-id " + binding.GameId + " --tool-root " + Quote(binding.ToolRoot),
                WorkingDirectory = binding.ToolRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            start.EnvironmentVariables["YEYU_GAMER_STAGE_FILE"] = stageFile;
            start.EnvironmentVariables["YEYU_GAMER_SELECTED_OPERATIONS"] = Json.Serialize(operations);
            using (Process process = new Process { StartInfo = start })
            {
                DataReceivedEventHandler collect = (sender, eventArgs) => {
                    if (eventArgs.Data == null) return;
                    lock (output) {
                        if (output.Length > 1048576) output.Remove(0, output.Length - 524288);
                        output.AppendLine(eventArgs.Data);
                    }
                };
                process.OutputDataReceived += collect;
                process.ErrorDataReceived += collect;
                if (!process.Start()) throw new InvalidOperationException("classic_driver_not_started");
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                var seenLines = new HashSet<string>(StringComparer.Ordinal);
                while (!process.WaitForExit(150)) DrainStages(stageFile, todos, request, binding.GameId, ref sequence, seenLines);
                process.WaitForExit();
                DrainStages(stageFile, todos, request, binding.GameId, ref sequence, seenLines);
                lock (output) { detail = "driverExit=" + process.ExitCode + "; tail=" + Tail(output.ToString(), 1200); }
                return process.ExitCode;
            }
        }

        private static void DrainStages(string path, List<Todo> todos, IDictionary<string, object> request, string gameId, ref int sequence, HashSet<string> seen = null)
        {
            if (!File.Exists(path)) return;
            if (seen == null) seen = new HashSet<string>(StringComparer.Ordinal);
            string[] lines;
            try { lines = ReadSharedLines(path); }
            catch (IOException) { return; }
            foreach (string line in lines)
            {
                if (String.IsNullOrWhiteSpace(line) || !seen.Add(line)) continue;
                IDictionary<string, object> record;
                try { record = Json.DeserializeObject(line) as IDictionary<string, object>; }
                catch { continue; }
                string operation = record == null ? null : record["operation"] as string;
                string state = record == null ? null : record["state"] as string;
                string detail = record != null && record.ContainsKey("detail") ? record["detail"] as string : String.Empty;
                Todo todo = todos.FirstOrDefault(item => item.Operation == operation);
                if (todo == null || String.IsNullOrEmpty(state)) continue;
                if (state == "started" && !todo.Started)
                {
                    todo.Started = true;
                    Emit(Base(request, gameId, "todo_attempt_started", sequence++, new Dictionary<string, object> {
                        { "todoInstanceId", todo.InstanceId }, { "todoAttemptId", todo.AttemptId }, { "attemptNo", todo.AttemptNo }, { "operation", todo.Operation }
                    }));
                }
                else if ((state == "completed" || state == "failed" || state == "skipped" || state == "human_required") && !todo.Terminal)
                {
                    string reasonCode = state == "completed" ? "upstream_stage_completed" : state == "skipped" ? "upstream_stage_not_needed" : state == "human_required" ? "human_gate_detected" : "upstream_stage_failed";
                    Terminal(todo, state == "completed" ? "completed" : state, reasonCode, detail ?? String.Empty, request, gameId, ref sequence);
                }
            }
        }

        private static string[] ReadSharedLines(string path)
        {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            using (var reader = new StreamReader(stream, Encoding.UTF8, true))
                return reader.ReadToEnd().Split(new string[] { "\r\n", "\n" }, StringSplitOptions.RemoveEmptyEntries);
        }

        private static void Terminal(Todo todo, string status, string reasonCode, string reason, IDictionary<string, object> request, string gameId, ref int sequence)
        {
            if (!todo.Started) return;
            string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            string name = gameId.ToLowerInvariant() + "-selected-" + Guid.NewGuid().ToString("N") + ".txt";
            string path = Path.Combine(staging, name);
            File.WriteAllText(path, "operation=" + todo.Operation + "; status=" + status + "; " + reason, new UTF8Encoding(false));
            string artifactId = Guid.NewGuid().ToString();
            Emit(Base(request, gameId, "artifact_staged", sequence++, new Dictionary<string, object> {
                { "todoInstanceId", todo.InstanceId }, { "todoAttemptId", todo.AttemptId }, { "artifactId", artifactId }, { "kind", "tool-log-outcome" },
                { "fileName", name }, { "mimeType", "text/plain" }, { "sizeBytes", new FileInfo(path).Length }, { "sha256", Hash(path) }, { "capturedAt", DateTime.UtcNow.ToString("o") }
            }));
            Emit(Base(request, gameId, "todo_terminal", sequence++, new Dictionary<string, object> {
                { "todoInstanceId", todo.InstanceId }, { "todoAttemptId", todo.AttemptId }, { "status", status }, { "reasonCode", reasonCode },
                { "reason", ProtocolText(reason) }, { "retryable", status == "failed" }, { "evidenceArtifactIds", new [] { artifactId } }
            }));
            todo.Terminal = true;
            todo.Completed = status == "completed";
            todo.Status = status;
        }

        private static string ProtocolText(string value)
        {
            if (String.IsNullOrWhiteSpace(value)) return "upstream tool supplied no detail";
            var output = new StringBuilder(Math.Min(value.Length, 1200));
            bool pendingSpace = false;
            foreach (char character in value)
            {
                if (Char.IsControl(character) || Char.IsWhiteSpace(character))
                {
                    pendingSpace = output.Length > 0;
                    continue;
                }
                if (pendingSpace && output.Length < 1200) output.Append(' ');
                pendingSpace = false;
                if (output.Length >= 1200) break;
                output.Append(character);
            }
            return output.Length == 0 ? "upstream tool supplied no printable detail" : output.ToString();
        }

        private static Binding LoadBinding(string expectedGameId)
        {
            string managerPath = Environment.GetEnvironmentVariable("YEYU_GAMER_INSTALLATION_BINDING_PATH");
            string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            string full = Path.GetFullPath(managerPath ?? String.Empty);
            if (String.IsNullOrEmpty(staging) || !File.Exists(full) || !String.Equals(Path.GetFileName(full), "installation-binding.json", StringComparison.OrdinalIgnoreCase) ||
                !full.StartsWith(Path.GetFullPath(staging).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("manager_installation_binding_invalid");
            IDictionary<string, object> manager = Json.DeserializeObject(File.ReadAllText(full, Encoding.UTF8)) as IDictionary<string, object>;
            if (manager == null || (manager["gameId"] as string) != expectedGameId || !(manager["toolPath"] is string) || !(manager["gamePath"] is string)) throw new InvalidOperationException("manager_installation_binding_invalid");
            IDictionary<string, object> trusted = Json.DeserializeObject(File.ReadAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "tool-binding.json"), Encoding.UTF8)) as IDictionary<string, object>;
            IDictionary<string, object> bindings = trusted == null ? null : trusted["bindings"] as IDictionary<string, object>;
            IDictionary<string, object> item = bindings == null ? null : bindings[expectedGameId] as IDictionary<string, object>;
            if (item == null) throw new InvalidOperationException("trusted_binding_missing");
            string toolRoot = Path.GetFullPath(manager["toolPath"] as string);
            string gamePath = Path.GetFullPath(manager["gamePath"] as string);
            string trustedTool = Path.GetFullPath(item["toolRoot"] as string);
            string trustedGame = Path.GetFullPath(item["gamePath"] as string);
            string python = Path.GetFullPath(item["python"] as string);
            if (toolRoot.StartsWith("\\") || gamePath.StartsWith("\\") || python.StartsWith("\\") ||
                !String.Equals(toolRoot.TrimEnd('\\'), trustedTool.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(gamePath, trustedGame, StringComparison.OrdinalIgnoreCase) || !Directory.Exists(toolRoot) || !File.Exists(gamePath) || !File.Exists(python)) throw new InvalidOperationException("configured_installation_missing_or_untrusted");
            return new Binding { GameId = expectedGameId, ToolRoot = toolRoot, GamePath = gamePath, Python = python };
        }

        private static bool SupportedGame(string gameId) { return gameId == "PGR" || gameId == "ZZZ" || gameId == "NIKKE"; }
        private static bool AllowedOperation(string gameId, string operation)
        {
            if (gameId == "PGR") return new [] { "attach-home", "claim-serum", "dorm", "simulation-field", "maintainer-action", "claim-daily-tasks", "battle-pass-free-track" }.Contains(operation);
            if (gameId == "ZZZ") return new [] { "attach-home", "coffee", "scratch-card", "trigrams-collection", "suibian-temple", "random-play", "charge-plan", "city-fund-free-claim", "engagement-reward" }.Contains(operation);
            return new [] { "attach-lobby", "outpost", "dispatch-friend" }.Contains(operation);
        }
        private static Dictionary<string, string> ParseArguments(string[] args)
        {
            if (args == null || args.Length != 8) return null;
            var result = new Dictionary<string, string>(StringComparer.Ordinal);
            for (int index = 0; index < args.Length; index += 2) { if (!args[index].StartsWith("--") || result.ContainsKey(args[index])) return null; result.Add(args[index], args[index + 1]); }
            return result.Count == 4 && result.ContainsKey("--protocol-version") && result.ContainsKey("--run-id") && result.ContainsKey("--run-attempt-id") && result.ContainsKey("--game-id") ? result : null;
        }
        private static bool MatchesRequest(IDictionary<string, object> request, Dictionary<string, string> options)
        {
            return request != null && (request["protocolVersion"] as string) == ProtocolVersion && (request["gameId"] as string) == options["--game-id"] &&
                (request["runId"] as string) == options["--run-id"] && (request["runAttemptId"] as string) == options["--run-attempt-id"] && request["preserveClientOnStop"] is bool && (bool)request["preserveClientOnStop"];
        }
        private static int AttemptNumber(IDictionary<string, object> todo)
        {
            object value = todo == null || !todo.ContainsKey("priorAttempts") ? null : todo["priorAttempts"];
            if (value is int && (int)value >= 0) return checked((int)value + 1);
            if (value is decimal && decimal.Truncate((decimal)value) == (decimal)value && (decimal)value >= 0 && (decimal)value < Int32.MaxValue) return checked((int)(decimal)value + 1);
            throw new InvalidOperationException("prior_attempts_invalid");
        }
        private static Dictionary<string, object> Base(IDictionary<string, object> request, string gameId, string eventType, int sequence, Dictionary<string, object> extra)
        {
            var value = new Dictionary<string, object> { {"schemaVersion",1}, {"protocolVersion",ProtocolVersion}, {"eventType",eventType}, {"sequence",sequence}, {"runId",request["runId"]}, {"runAttemptId",request["runAttemptId"]}, {"fencingToken",request["fencingToken"]}, {"gameId",gameId}, {"at",DateTime.UtcNow.ToString("o")} };
            foreach (KeyValuePair<string, object> item in extra) value[item.Key] = item.Value;
            return value;
        }
        private static void Emit(Dictionary<string, object> value) { Console.Out.WriteLine(Json.Serialize(value)); Console.Out.Flush(); }
        private static string Hash(string path) { using (FileStream stream = File.OpenRead(path)) using (SHA256 sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", String.Empty).ToLowerInvariant(); }
        private static string Quote(string value) { return "\"" + value.Replace("\"", "\\\"") + "\""; }
        private static string Tail(string value, int max) { return value.Length <= max ? value : value.Substring(value.Length - max); }
        private static bool Probe()
        {
            try
            {
                string path = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "tool-binding.json");
                IDictionary<string, object> trusted = Json.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as IDictionary<string, object>;
                IDictionary<string, object> bindings = trusted == null ? null : trusted["bindings"] as IDictionary<string, object>;
                foreach (string gameId in new [] { "PGR", "ZZZ", "NIKKE" })
                {
                    IDictionary<string, object> item = bindings == null ? null : bindings[gameId] as IDictionary<string, object>;
                    if (item == null) return false;
                    string toolRoot = Path.GetFullPath(item["toolRoot"] as string);
                    string gamePath = Path.GetFullPath(item["gamePath"] as string);
                    string python = Path.GetFullPath(item["python"] as string);
                    if (toolRoot.StartsWith("\\") || gamePath.StartsWith("\\") || python.StartsWith("\\") ||
                        !Directory.Exists(toolRoot) || !File.Exists(gamePath) || !File.Exists(python)) return false;
                }
                return true;
            }
            catch { return false; }
        }
        private static string PackageVersion() { try { var value = Json.DeserializeObject(File.ReadAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "install-manifest.json"))) as IDictionary<string, object>; return value["packageVersion"] as string; } catch { return "unknown"; } }
        private static string PackageDigest() { try { string root = AppDomain.CurrentDomain.BaseDirectory; byte[] raw = File.ReadAllBytes(Path.Combine(root, "install-manifest.json")); var document = Json.DeserializeObject(Encoding.UTF8.GetString(raw)) as IDictionary<string, object>; object[] files = document["files"] as object[]; var entries = new SortedDictionary<string, string>(StringComparer.Ordinal); foreach (object item in files) { var file = item as IDictionary<string, object>; entries.Add(file["path"] as string,file["sha256"] as string); } using (SHA256 sha = SHA256.Create()) using (var stream = new MemoryStream()) { stream.Write(raw, 0, raw.Length); foreach (var entry in entries) { byte[] path = Encoding.UTF8.GetBytes(entry.Key); byte[] hash = Encoding.ASCII.GetBytes(entry.Value); stream.Write(path, 0, path.Length); stream.Write(hash, 0, hash.Length); } return "sha256:" + BitConverter.ToString(sha.ComputeHash(stream.ToArray())).Replace("-", String.Empty).ToLowerInvariant(); } } catch (Exception error) { throw new InvalidDataException("package_digest_unavailable", error); } }
        private static string TerminalDigest(IDictionary<string, object> request, string status, string[] attempted, string[] completed, string[] unresolved, int exitCode) { string payload=(request["runId"] as string)+"\n"+(request["runAttemptId"] as string)+"\n"+status+"\n"+String.Join("\n",attempted)+"\n--completed--\n"+String.Join("\n",completed)+"\n--unresolved--\n"+String.Join("\n",unresolved)+"\n"+exitCode; using(var sha=SHA256.Create()){return "sha256:"+BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(payload))).Replace("-",String.Empty).ToLowerInvariant();} }
        private static int SetupFailure(IDictionary<string, object> request, object[] ids, string reason)
        {
            string gameId = request["gameId"] as string;
            Emit(Base(request, gameId, "hello", 0, new Dictionary<string, object> { {"packageId",PackageId}, {"packageVersion",PackageVersion()}, {"packageDigest",PackageDigest()}, {"runnerPid",Process.GetCurrentProcess().Id}, {"acceptedTodoInstanceIds",ids} }));
            string[] unresolved = ids.Cast<string>().ToArray();
            Emit(Base(request, gameId, "run_terminal", 1, new Dictionary<string, object> { {"status","review_required"}, {"transportOutcome","clean"}, {"attemptedTodoInstanceIds",new string[0]}, {"completedTodoInstanceIds",new string[0]}, {"unresolvedTodoInstanceIds",ids}, {"terminalEventDigest",TerminalDigest(request,"review_required",new string[0],new string[0],unresolved,0)}, {"exitCode",0} }));
            return 0;
        }
    }
}
