using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;

namespace YeYuGamer.FgoAdapter
{
    internal sealed class RunnerFailure : Exception
    {
        public string Code { get; private set; }
        public RunnerFailure(string code, string message) : base(message) { Code = code; }
    }

    internal sealed class TodoTarget
    {
        public string TodoInstanceId;
        public string Operation;
        public int PriorAttempts;
    }

    internal sealed class ExecuteRequest
    {
        public string RunId;
        public string RunAttemptId;
        public string FencingToken;
        public string GameId;
        public DateTimeOffset IssuedAt;
        public DateTimeOffset ExpiresAt;
        public List<TodoTarget> Todos;

        public static ExecuteRequest Parse(string text, RunnerArguments args)
        {
            IDictionary<string, object> value = JsonContract.Object(text, "execute request", 262144);
            JsonContract.ExactKeys(value, new string[]
            {
                "schemaVersion", "protocolVersion", "requestType", "runId", "runAttemptId",
                "fencingToken", "gameId", "cadence", "managerStateVersion", "catalogVersion",
                "policyDigest", "issuedAt", "expiresAt", "timeoutSeconds", "preserveClientOnStop",
                "executableTodoInstanceIds", "todos"
            }, "execute request");
            if (JsonContract.Integer(value["schemaVersion"], "schemaVersion", 1, 1) != 1 ||
                JsonContract.Text(value["protocolVersion"], "protocolVersion", 8) != Protocol.Version ||
                JsonContract.Text(value["requestType"], "requestType", 16) != "execute")
                throw new RunnerFailure("unsupported_request", "Only the fixed Adapter execute request is supported.");
            string runId = JsonContract.Uuid(value["runId"], "runId");
            string attemptId = JsonContract.Uuid(value["runAttemptId"], "runAttemptId");
            string gameId = JsonContract.Text(value["gameId"], "gameId", 32);
            if (runId != args.RunId || attemptId != args.RunAttemptId || gameId != "FGO")
                throw new RunnerFailure("request_scope_mismatch", "The request differs from the fixed Host scope.");
            if (JsonContract.Text(value["cadence"], "cadence", 16) != "daily")
                throw new RunnerFailure("cadence_not_supported", "Only FGO daily cadence is accepted.");
            if (!(value["preserveClientOnStop"] is bool) || !(bool)value["preserveClientOnStop"])
                throw new RunnerFailure("unsafe_stop_policy", "preserveClientOnStop must be true.");
            string token = JsonContract.Text(value["fencingToken"], "fencingToken", 256);
            if (!Regex.IsMatch(token, "^[A-Za-z0-9._~-]{32,256}$", RegexOptions.CultureInvariant))
                throw new RunnerFailure("invalid_fencing_token", "The fencing token is malformed.");
            string policyDigest = JsonContract.Text(value["policyDigest"], "policyDigest", 71);
            if (!Regex.IsMatch(policyDigest, "^(sha256:)?[0-9a-f]{64}$", RegexOptions.CultureInvariant))
                throw new RunnerFailure("invalid_policy_digest", "The policy digest is malformed.");
            JsonContract.Identifier(value["catalogVersion"], "catalogVersion");
            JsonContract.Integer(value["managerStateVersion"], "managerStateVersion", 0, Int64.MaxValue);
            DateTimeOffset issued = JsonContract.Time(value["issuedAt"], "issuedAt");
            DateTimeOffset expires = JsonContract.Time(value["expiresAt"], "expiresAt");
            if (expires <= issued || DateTimeOffset.UtcNow < issued.AddSeconds(-5) || DateTimeOffset.UtcNow >= expires)
                throw new RunnerFailure("stale_lease", "The execution lease is invalid or stale.");
            long timeout = JsonContract.Integer(value["timeoutSeconds"], "timeoutSeconds", 1, 86400);
            if (timeout > (expires - issued).TotalSeconds)
                throw new RunnerFailure("invalid_lease", "The timeout exceeds the execution lease.");

            List<object> idValues = JsonContract.Array(value["executableTodoInstanceIds"], "executableTodoInstanceIds", 1, 8);
            List<string> ids = idValues.Select(delegate(object item) { return JsonContract.TodoId(item, "executableTodoInstanceIds"); }).ToList();
            if (ids.Distinct(StringComparer.Ordinal).Count() != ids.Count)
                throw new RunnerFailure("duplicate_todo", "Executable Todo IDs must be unique.");
            List<object> documents = JsonContract.Array(value["todos"], "todos", 1, 8);
            List<TodoTarget> todos = documents.Select(ParseTodo).ToList();
            if (!ids.SequenceEqual(todos.Select(delegate(TodoTarget item) { return item.TodoInstanceId; }), StringComparer.Ordinal))
                throw new RunnerFailure("todo_scope_mismatch", "Todo documents differ from executable IDs.");
            return new ExecuteRequest
            {
                RunId = runId, RunAttemptId = attemptId, FencingToken = token, GameId = gameId,
                IssuedAt = issued, ExpiresAt = expires, Todos = todos
            };
        }

        private static TodoTarget ParseTodo(object document)
        {
            IDictionary<string, object> value = JsonContract.AsObject(document, "Todo target");
            JsonContract.ExactKeys(value, new string[]
            {
                "todoInstanceId", "todoDefinitionId", "definitionVersion", "operation", "risk",
                "adapterCapabilityRef", "priorAttempts", "executionDisposition"
            }, "Todo target");
            string operation = JsonContract.Identifier(value["operation"], "operation");
            string definition;
            if (!Protocol.Operations.TryGetValue(operation, out definition) ||
                JsonContract.Identifier(value["todoDefinitionId"], "todoDefinitionId") != definition)
                throw new RunnerFailure("operation_not_bound", "The Todo operation is not in the fixed FGO binding.");
            if (JsonContract.Text(value["risk"], "risk", 32) != "routine_action" ||
                JsonContract.Text(value["adapterCapabilityRef"], "adapterCapabilityRef", 128) != Protocol.Capability ||
                JsonContract.Text(value["executionDisposition"], "executionDisposition", 32) != "executable")
                throw new RunnerFailure("todo_not_executable", "The Todo risk, capability, or disposition is invalid.");
            JsonContract.Integer(value["definitionVersion"], "definitionVersion", 1, Int32.MaxValue);
            return new TodoTarget
            {
                TodoInstanceId = JsonContract.TodoId(value["todoInstanceId"], "todoInstanceId"),
                Operation = operation,
                PriorAttempts = checked((int)JsonContract.Integer(value["priorAttempts"], "priorAttempts", 0, Int32.MaxValue))
            };
        }
    }

    internal sealed class RunnerArguments
    {
        public bool Probe;
        public string RunId;
        public string RunAttemptId;

        public static RunnerArguments Parse(string[] args)
        {
            if (args.Length == 1 && args[0] == "--probe-binding") return new RunnerArguments { Probe = true };
            if (args.Length != 8) throw new RunnerFailure("invalid_arguments", "The fixed execute arguments are required.");
            Dictionary<string, string> values = new Dictionary<string, string>(StringComparer.Ordinal);
            for (int index = 0; index < args.Length; index += 2)
            {
                if (!args[index].StartsWith("--", StringComparison.Ordinal) || values.ContainsKey(args[index]))
                    throw new RunnerFailure("invalid_arguments", "Execute arguments are malformed.");
                values.Add(args[index], args[index + 1]);
            }
            JsonContract.ExactKeys(values, new string[] { "--protocol-version", "--run-id", "--run-attempt-id", "--game-id" }, "arguments");
            if (values["--protocol-version"] != Protocol.Version || values["--game-id"] != "FGO")
                throw new RunnerFailure("argument_scope_mismatch", "Only FGO protocol v1.1 is supported.");
            return new RunnerArguments
            {
                RunId = JsonContract.Uuid(values["--run-id"], "runId"),
                RunAttemptId = JsonContract.Uuid(values["--run-attempt-id"], "runAttemptId")
            };
        }
    }

    internal static class Protocol
    {
        public const string Version = "1.1";
        public const string PackageId = "legacy-night-rain-gamer";
        public const string Capability = "game.daily.run@1.0";
        public const string MaaFgoBaseUrl = "http://127.0.0.1:5566";
        public static readonly Dictionary<string, string> Operations = new Dictionary<string, string>(StringComparer.Ordinal)
        {
            { "attach-home", "todo.v1.fgo.daily.attach-home" },
            { "three-10ap-quests", "todo.v1.fgo.daily.three-10ap-quests" }
        };
    }

    internal sealed class PackageIdentity
    {
        public string Version;
        public string Digest;

        public static PackageIdentity Load(string root)
        {
            string manifestPath = Path.Combine(root, "install-manifest.json");
            byte[] raw = File.ReadAllBytes(manifestPath);
            IDictionary<string, object> manifest = JsonContract.Object(JsonContract.Utf8(raw), "package manifest", 65536);
            if (JsonContract.Text(manifest["packageId"], "packageId", 128) != Protocol.PackageId)
                throw new RunnerFailure("package_scope_mismatch", "The package ID is invalid.");
            string version = JsonContract.Identifier(manifest["packageVersion"], "packageVersion");
            Dictionary<string, string> hashes = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (object document in JsonContract.Array(manifest["files"], "files", 2, 16))
            {
                IDictionary<string, object> item = JsonContract.AsObject(document, "file");
                JsonContract.ExactKeys(item, new string[] { "path", "sha256", "sizeBytes" }, "file");
                string relative = JsonContract.RelativePath(item["path"], "path");
                string full = JsonContract.ContainedFile(root, relative);
                string hash = JsonContract.Text(item["sha256"], "sha256", 64);
                if (!Regex.IsMatch(hash, "^[0-9a-f]{64}$") || JsonContract.Sha256File(full) != hash ||
                    new FileInfo(full).Length != JsonContract.Integer(item["sizeBytes"], "sizeBytes", 1, Int64.MaxValue))
                    throw new RunnerFailure("package_file_mismatch", "A package file differs from its manifest.");
                hashes.Add(relative, hash);
            }
            HashSet<string> actual = new HashSet<string>(Directory.GetFiles(root, "*", SearchOption.AllDirectories)
                .Select(delegate(string path) { return path.Substring(root.TrimEnd('\\').Length + 1).Replace('\\', '/'); }), StringComparer.Ordinal);
            HashSet<string> expected = new HashSet<string>(hashes.Keys, StringComparer.Ordinal);
            expected.Add("install-manifest.json");
            if (!actual.SetEquals(expected)) throw new RunnerFailure("package_file_extras", "The package contains unexpected files.");
            using (SHA256 sha = SHA256.Create())
            using (MemoryStream stream = new MemoryStream())
            {
                stream.Write(raw, 0, raw.Length);
                foreach (string relative in hashes.Keys.OrderBy(delegate(string item) { return item; }, StringComparer.Ordinal))
                {
                    byte[] pathBytes = Encoding.UTF8.GetBytes(relative);
                    byte[] hashBytes = Encoding.ASCII.GetBytes(hashes[relative]);
                    stream.Write(pathBytes, 0, pathBytes.Length);
                    stream.Write(hashBytes, 0, hashBytes.Length);
                }
                stream.Position = 0;
                return new PackageIdentity { Version = version, Digest = JsonContract.Hex(sha.ComputeHash(stream)) };
            }
        }
    }

    internal sealed class ToolBinding
    {
        public string Root;
        public string Launcher;
        public string LogFile;

        public static ToolBinding Load(string root)
        {
            IDictionary<string, object> value = JsonContract.Object(File.ReadAllText(Path.Combine(root, "tool-binding.json"), Encoding.UTF8), "tool binding", 65536);
            JsonContract.ExactKeys(value, new string[] { "schemaVersion", "bindingId", "gameId", "tool", "operations", "safety" }, "tool binding");
            if (JsonContract.Integer(value["schemaVersion"], "schemaVersion", 1, 1) != 1 ||
                JsonContract.Text(value["bindingId"], "bindingId", 128) != "fgo-maafgo-formal-v1" ||
                JsonContract.Text(value["gameId"], "gameId", 32) != "FGO")
                throw new RunnerFailure("binding_scope_mismatch", "The FGO binding scope is invalid.");
            IDictionary<string, object> tool = JsonContract.AsObject(value["tool"], "tool");
            JsonContract.ExactKeys(tool, new string[] { "root", "launcher", "launcherSha256", "task", "taskSha256", "log" }, "tool");
            string toolRoot = Path.GetFullPath(JsonContract.Text(tool["root"], "root", 1024));
            if (!Path.IsPathRooted(toolRoot) || toolRoot.StartsWith("\\\\", StringComparison.Ordinal) || !Directory.Exists(toolRoot) || JsonContract.IsReparse(toolRoot))
                throw new RunnerFailure("unsafe_tool_root", "MaaFgo must be installed on a local non-reparse directory.");
            string launcher = JsonContract.ContainedFile(toolRoot, JsonContract.RelativePath(tool["launcher"], "launcher"));
            string task = JsonContract.ContainedFile(toolRoot, JsonContract.RelativePath(tool["task"], "task"));
            if (JsonContract.Sha256File(launcher) != JsonContract.Text(tool["launcherSha256"], "launcherSha256", 64) ||
                JsonContract.Sha256File(task) != JsonContract.Text(tool["taskSha256"], "taskSha256", 64))
                throw new RunnerFailure("tool_binding_drift", "MaaFgo executable or selected task changed after the candidate build.");
            string taskText = File.ReadAllText(task, Encoding.UTF8);
            if (!taskText.Contains("\"allow_ap_recovery\": false") || !taskText.Contains("\"strict_result\": true"))
                throw new RunnerFailure("unsafe_tool_config", "MaaFgo must disable AP recovery and require a strict battle result.");
            IDictionary<string, object> operations = JsonContract.AsObject(value["operations"], "operations");
            JsonContract.ExactKeys(operations, Protocol.Operations.Keys, "operations");
            foreach (string operation in Protocol.Operations.Keys)
            {
                IDictionary<string, object> item = JsonContract.AsObject(operations[operation], operation);
                JsonContract.ExactKeys(item, new string[] { "mode", "reasonCode" }, operation);
                if (JsonContract.Text(item["mode"], "mode", 32) != "formal_gui")
                    throw new RunnerFailure("unsafe_operation_binding", "FGO operations must use the formal GUI binding.");
                JsonContract.Identifier(item["reasonCode"], "reasonCode");
            }
            IDictionary<string, object> safety = JsonContract.AsObject(value["safety"], "safety");
            JsonContract.ExactKeys(safety, new string[] { "allowAppleUse", "allowSaintQuartz", "allowSummon", "allowMailboxAssetActions", "formalGuiRequired" }, "safety");
            foreach (KeyValuePair<string, object> state in safety)
                if (!(state.Value is bool) || (state.Key == "formalGuiRequired" ? !(bool)state.Value : (bool)state.Value))
                    throw new RunnerFailure("unsafe_binding_policy", "The FGO safety policy is invalid.");
            string log = Path.GetFullPath(Path.Combine(toolRoot, JsonContract.RelativePath(tool["log"], "log")));
            if (!log.StartsWith(toolRoot.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))
                throw new RunnerFailure("unsafe_path", "MaaFgo log path escaped the tool root.");
            return new ToolBinding { Root = toolRoot, Launcher = launcher, LogFile = log };
        }
    }

    internal sealed class EventWriter
    {
        private readonly ExecuteRequest request;
        private readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 262144 };
        private readonly MemoryStream transcript = new MemoryStream();
        private long sequence;
        public EventWriter(ExecuteRequest request) { this.request = request; }

        public void Emit(string type, IDictionary<string, object> fields)
        {
            Dictionary<string, object> document = new Dictionary<string, object>(StringComparer.Ordinal)
            {
                { "schemaVersion", 1 }, { "protocolVersion", Protocol.Version }, { "eventType", type },
                { "sequence", sequence++ }, { "runId", request.RunId }, { "runAttemptId", request.RunAttemptId },
                { "fencingToken", request.FencingToken }, { "gameId", request.GameId }, { "at", Timestamp(DateTimeOffset.UtcNow) }
            };
            foreach (KeyValuePair<string, object> field in fields) document.Add(field.Key, field.Value);
            string line = json.Serialize(document);
            byte[] bytes = Encoding.UTF8.GetBytes(line + "\n");
            transcript.Write(bytes, 0, bytes.Length);
            Console.Out.WriteLine(line);
            Console.Out.Flush();
        }

        public string Digest()
        {
            using (SHA256 sha = SHA256.Create()) return "sha256:" + JsonContract.Hex(sha.ComputeHash(transcript.ToArray()));
        }

        public string TerminalDigest(string status, IEnumerable<string> attempted,
            IEnumerable<string> completed, IEnumerable<string> unresolved, int exitCode)
        {
            string payload = request.RunId + "\n" + request.RunAttemptId + "\n" + status + "\n" +
                String.Join("\n", attempted) + "\n--completed--\n" + String.Join("\n", completed) +
                "\n--unresolved--\n" + String.Join("\n", unresolved) + "\n" +
                exitCode.ToString(CultureInfo.InvariantCulture);
            using (SHA256 sha = SHA256.Create())
                return "sha256:" + JsonContract.Hex(sha.ComputeHash(Encoding.UTF8.GetBytes(payload)));
        }

        public static string Timestamp(DateTimeOffset value)
        {
            return value.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture);
        }
    }

    internal static class JsonContract
    {
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 262144 };
        private static readonly Regex IdentifierPattern = new Regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", RegexOptions.CultureInvariant);
        private static readonly Regex TodoPattern = new Regex("^todo-instance-([0-9a-f-]{36})$", RegexOptions.CultureInvariant);

        public static IDictionary<string, object> Object(string text, string context, int maximum)
        {
            if (String.IsNullOrEmpty(text) || Encoding.UTF8.GetByteCount(text) > maximum) throw new RunnerFailure("invalid_json", context + " has an invalid size.");
            try { return AsObject(Json.DeserializeObject(text), context); }
            catch (RunnerFailure) { throw; }
            catch { throw new RunnerFailure("invalid_json", context + " is not one JSON object."); }
        }
        public static IDictionary<string, object> AsObject(object value, string context)
        {
            IDictionary<string, object> result = value as IDictionary<string, object>;
            if (result == null) throw new RunnerFailure("invalid_schema", context + " must be an object.");
            return result;
        }
        public static void ExactKeys<T>(IDictionary<string, T> value, IEnumerable<string> keys, string context)
        {
            if (!new HashSet<string>(keys, StringComparer.Ordinal).SetEquals(value.Keys)) throw new RunnerFailure("invalid_schema", context + " fields differ from the fixed contract.");
        }
        public static string Text(object value, string field, int maximum)
        {
            string text = value as string;
            if (String.IsNullOrEmpty(text) || text.Length > maximum || text.Any(delegate(char item) { return item < 32; })) throw new RunnerFailure("invalid_schema", field + " is invalid.");
            return text;
        }
        public static string Identifier(object value, string field)
        {
            string text = Text(value, field, 128);
            if (!IdentifierPattern.IsMatch(text)) throw new RunnerFailure("invalid_schema", field + " is not an identifier.");
            return text;
        }
        public static string Uuid(object value, string field)
        {
            string text = Text(value, field, 36); Guid parsed;
            if (!Guid.TryParse(text, out parsed) || parsed.ToString() != text) throw new RunnerFailure("invalid_scope", field + " must be a canonical UUID.");
            return text;
        }
        public static string TodoId(object value, string field)
        {
            string text = Text(value, field, 50); Match match = TodoPattern.Match(text);
            if (!match.Success) throw new RunnerFailure("invalid_scope", field + " is not a Todo instance ID.");
            Uuid(match.Groups[1].Value, field); return text;
        }
        public static long Integer(object value, string field, long minimum, long maximum)
        {
            if (value == null || value is bool) throw new RunnerFailure("invalid_schema", field + " is not an integer.");
            long result; try { result = Convert.ToInt64(value, CultureInfo.InvariantCulture); } catch { throw new RunnerFailure("invalid_schema", field + " is not an integer."); }
            if (result < minimum || result > maximum || (value is double && (double)value != result)) throw new RunnerFailure("invalid_schema", field + " is outside its bounds.");
            return result;
        }
        public static DateTimeOffset Time(object value, string field)
        {
            string text = Text(value, field, 64); DateTimeOffset parsed;
            if (!DateTimeOffset.TryParse(text, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out parsed) ||
                !(text.EndsWith("Z", StringComparison.Ordinal) || Regex.IsMatch(text, "[+-][0-9]{2}:[0-9]{2}$"))) throw new RunnerFailure("invalid_schema", field + " is not an offset timestamp.");
            return parsed;
        }
        public static List<object> Array(object value, string field, int minimum, int maximum)
        {
            IEnumerable source = value as IEnumerable;
            if (source == null || value is string || value is IDictionary) throw new RunnerFailure("invalid_schema", field + " must be an array.");
            List<object> result = new List<object>(); foreach (object item in source) result.Add(item);
            if (result.Count < minimum || result.Count > maximum) throw new RunnerFailure("invalid_schema", field + " has an invalid length.");
            return result;
        }
        public static string RelativePath(object value, string field)
        {
            string text = Text(value, field, 256).Replace('\\', '/');
            if (Path.IsPathRooted(text) || text.Split('/').Any(delegate(string part) { return part.Length == 0 || part == "." || part == ".."; })) throw new RunnerFailure("unsafe_path", field + " is unsafe.");
            return text;
        }
        public static string ContainedFile(string root, string relative)
        {
            string canonical = Path.GetFullPath(root).TrimEnd('\\') + "\\";
            string full = Path.GetFullPath(Path.Combine(canonical, relative.Replace('/', '\\')));
            if (!full.StartsWith(canonical, StringComparison.OrdinalIgnoreCase) || !File.Exists(full) || IsReparse(full)) throw new RunnerFailure("unsafe_path", "A bound file is unavailable.");
            return full;
        }
        public static bool IsReparse(string path) { return (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0; }
        public static string Sha256File(string path) { using (SHA256 sha = SHA256.Create()) using (FileStream stream = File.OpenRead(path)) return Hex(sha.ComputeHash(stream)); }
        public static string Hex(byte[] bytes) { return BitConverter.ToString(bytes).Replace("-", String.Empty).ToLowerInvariant(); }
        public static string Utf8(byte[] bytes) { try { return new UTF8Encoding(false, true).GetString(bytes); } catch { throw new RunnerFailure("invalid_utf8", "File is not UTF-8."); } }
    }

    internal static class Program
    {
        public static int Main(string[] args)
        {
            try
            {
                RunnerArguments parsed = RunnerArguments.Parse(args);
                string root = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory).TrimEnd('\\');
                if (root.StartsWith("\\\\", StringComparison.Ordinal) || JsonContract.IsReparse(root)) throw new RunnerFailure("unsafe_package_root", "Package root must be local.");
                PackageIdentity package = PackageIdentity.Load(root);
                ToolBinding binding = ToolBinding.Load(root);
                if (parsed.Probe)
                {
                    Console.Out.WriteLine(new JavaScriptSerializer().Serialize(new Dictionary<string, object>
                    {
                        { "schemaVersion", 1 }, { "probe", "binding" }, { "ok", true }, { "gameId", "FGO" },
                        { "packageVersion", package.Version }, { "packageDigest", package.Digest },
                        { "executionMode", "maafgo-formal-gui" }, { "processStarted", false }
                    }));
                    return 0;
                }
                ExecuteRequest request = ExecuteRequest.Parse(ReadLineBounded(Console.In, 262144), parsed);
                string staging = ValidateStaging(request);
                EventWriter events = new EventWriter(request);
                events.Emit("hello", new Dictionary<string, object>
                {
                    { "packageId", Protocol.PackageId }, { "packageVersion", package.Version }, { "packageDigest", package.Digest },
                    { "runnerPid", Process.GetCurrentProcess().Id }, { "acceptedTodoInstanceIds", request.Todos.Select(delegate(TodoTarget item) { return item.TodoInstanceId; }).ToArray() }
                });
                List<string> attempted = new List<string>();
                List<string> completed = new List<string>();
                Dictionary<string, string> attemptByTodo = new Dictionary<string, string>(StringComparer.Ordinal);
                // MaaFgo exposes the selected daily as one formal composite
                // command.  Announce every covered Todo before that command
                // begins so Manager's pre-step captures cannot accidentally be
                // taken from the already-completed reward screen.
                foreach (TodoTarget todo in request.Todos)
                {
                    string attemptId = Guid.NewGuid().ToString();
                    attemptByTodo[todo.TodoInstanceId] = attemptId;
                    attempted.Add(todo.TodoInstanceId);
                    events.Emit("todo_attempt_started", new Dictionary<string, object>
                    {
                        { "todoInstanceId", todo.TodoInstanceId }, { "todoAttemptId", attemptId }, { "attemptNo", todo.PriorAttempts + 1 }, { "operation", todo.Operation }
                    });
                }
                string executionLog;
                bool toolSucceeded = RunFormalMaaFgo(binding, request, out executionLog);
                foreach (TodoTarget todo in request.Todos)
                {
                    string attemptId = attemptByTodo[todo.TodoInstanceId];
                    bool operationSucceeded = toolSucceeded &&
                        (todo.Operation == "attach-home" || executionLog.Contains("日常战斗"));
                    string code = operationSucceeded ? "maafgo_task_succeeded" : "maafgo_terminal_evidence_missing";
                    string reason = operationSucceeded
                        ? "The formal MaaFgo GUI reported the selected task as succeeded."
                        : "The formal MaaFgo GUI did not produce the required strict terminal marker.";
                    string artifactId = StageDiagnostic(staging, todo, attemptId, events, executionLog);
                    string status = operationSucceeded ? "completed" : "review_required";
                    if (operationSucceeded) completed.Add(todo.TodoInstanceId);
                    events.Emit("todo_terminal", new Dictionary<string, object>
                    {
                        { "todoInstanceId", todo.TodoInstanceId }, { "todoAttemptId", attemptId }, { "status", status },
                        { "reasonCode", code }, { "reason", reason }, { "retryable", !operationSucceeded }, { "evidenceArtifactIds", new string[] { artifactId } }
                    });
                }
                string runStatus = completed.Count == request.Todos.Count ? "completed" : "review_required";
                string[] unresolved = request.Todos.Select(delegate(TodoTarget item) { return item.TodoInstanceId; })
                    .Where(delegate(string id) { return !completed.Contains(id); }).ToArray();
                events.Emit("run_terminal", new Dictionary<string, object>
                {
                    { "status", runStatus }, { "transportOutcome", "clean" },
                    { "attemptedTodoInstanceIds", attempted.ToArray() }, { "completedTodoInstanceIds", completed.ToArray() },
                    { "unresolvedTodoInstanceIds", unresolved },
                    { "terminalEventDigest", events.TerminalDigest(runStatus, attempted, completed, unresolved, 0) }, { "exitCode", 0 }
                });
                return 0;
            }
            catch (RunnerFailure error) { Console.Error.WriteLine(error.Code); return 64; }
            catch (Exception error) { Console.Error.WriteLine("runner_error:" + error.GetType().Name); return 70; }
        }

        private static bool RunFormalMaaFgo(ToolBinding binding, ExecuteRequest request, out string evidence)
        {
            long baseline = File.Exists(binding.LogFile) ? new FileInfo(binding.LogFile).Length : 0;
            bool running = IsFormalToolRunning(binding.Launcher);
            if (!running)
            {
                ProcessStartInfo start = new ProcessStartInfo(binding.Launcher) { WorkingDirectory = binding.Root, UseShellExecute = true };
                Process.Start(start);
            }
            DateTimeOffset apiDeadline = DateTimeOffset.UtcNow.AddMinutes(3);
            bool apiReady = false;
            while (DateTimeOffset.UtcNow < apiDeadline)
            {
                try
                {
                    Http("GET", Protocol.MaaFgoBaseUrl + "/api/device/state", null);
                    apiReady = true;
                    break;
                }
                catch { Thread.Sleep(1000); }
            }
            if (!apiReady)
            {
                evidence = "formal_gui_api_unavailable: MWU did not expose its formal API before the startup deadline.";
                return false;
            }
            Http("POST", Protocol.MaaFgoBaseUrl + "/api/resource?name=" + Uri.EscapeDataString("B服(小米/应用宝服)"), null);
            string devicePayload = "{\"type\":\"Adb\",\"controller_name\":\"安卓设备\",\"name\":\"雷电模拟器-LDPlayer\"," +
                "\"adb_path\":\"C:\\\\Game\\\\LDPlayer9\\\\adb.exe\",\"address\":\"emulator-5554\"," +
                "\"screencap_methods\":\"64\",\"input_methods\":\"18446744073709551607\"," +
                "\"config\":{\"extras\":{\"ld\":{\"enable\":true,\"index\":0,\"path\":\"C:/Game/LDPlayer9\"}}}}";
            Http("POST", Protocol.MaaFgoBaseUrl + "/api/device", devicePayload);
            DateTimeOffset deviceDeadline = DateTimeOffset.UtcNow.AddSeconds(30);
            bool deviceReady = false;
            while (DateTimeOffset.UtcNow < deviceDeadline)
            {
                string state = Http("GET", Protocol.MaaFgoBaseUrl + "/api/device/state", null);
                if (state.Contains("\"connected\":true") && state.Contains("B服(小米/应用宝服)"))
                {
                    deviceReady = true;
                    break;
                }
                Thread.Sleep(500);
            }
            if (!deviceReady)
            {
                evidence = "formal_gui_device_connect_failed: MWU did not confirm the fixed LDPlayer and B-server resource.";
                return false;
            }
            string payload = "{\"task_list\":[\"登录识别\",\"启动bbc\",\"日常战斗\"],\"task_options\":{" +
                "\"登录识别\":{\"包名\":\"B服\"}," +
                "\"启动bbc\":{\"connect_method\":\"ldplayer\",\"ld_path\":{\"ld_path\":\"C:\\\\Game\\\\LDPlayer9\"},\"ld_index\":{\"ld_index\":\"0\"}}," +
                "\"日常战斗\":{\"chapter\":\"七丘之城\",\"七丘之城\":\"罗马的地平线\",\"team_index\":\"默认队伍\",\"team_config\":\"bbc_team_config\",\"bbc_team_config\":\"BBA_枪奶光_小恩_妖兰_水莉莉丝——冠位枪.json\",\"chaldea_team_config\":{\"chaldea_import_source\":\"\"},\"bbc_run_count\":{\"run_count\":\"3\"},\"apple_type\":\"copper\",\"战前吃苹果\":\"none\",\"bbc_battle_type\":\"连续出击(或强化本)\",\"support_order_mismatch\":\"ignore\",\"team_config_error\":\"ignore\"}},\"preTasks\":[]}";
            try { Http("POST", Protocol.MaaFgoBaseUrl + "/api/start", payload); }
            catch (Exception error) { evidence = "formal_gui_api_start_failed: " + error.Message; return false; }
            DateTimeOffset deadline = request.ExpiresAt.AddSeconds(-5);
            DateTimeOffset nextHealthCheck = DateTimeOffset.UtcNow.AddSeconds(5);
            int unavailableHealthChecks = 0;
            while (DateTimeOffset.UtcNow < deadline)
            {
                string segment = ReadLogSegment(binding.LogFile, baseline);
                if (segment.Contains("Tasker.Task.Succeeded") && segment.Contains("日常战斗")) { evidence = segment; return true; }
                if (segment.Contains("Tasker.Task.Failed") && segment.Contains("日常战斗")) { evidence = segment; return false; }
                if (DateTimeOffset.UtcNow >= nextHealthCheck)
                {
                    nextHealthCheck = DateTimeOffset.UtcNow.AddSeconds(5);
                    try
                    {
                        Http("GET", Protocol.MaaFgoBaseUrl + "/api/device/state", null);
                        unavailableHealthChecks = 0;
                    }
                    catch
                    {
                        unavailableHealthChecks++;
                    }
                    if (unavailableHealthChecks >= 3 && !IsFormalToolRunning(binding.Launcher))
                    {
                        evidence = segment + Environment.NewLine +
                            "formal_gui_disappeared: MWU exited and its formal API stayed unavailable for three health checks.";
                        return false;
                    }
                }
                Thread.Sleep(1000);
            }
            evidence = ReadLogSegment(binding.LogFile, baseline);
            return false;
        }

        private static bool IsFormalToolRunning(string launcher)
        {
            return Process.GetProcessesByName(Path.GetFileNameWithoutExtension(launcher)).Any(delegate(Process item)
            {
                try { return String.Equals(Path.GetFullPath(item.MainModule.FileName), launcher, StringComparison.OrdinalIgnoreCase); }
                catch { return false; }
            });
        }

        private static string Http(string method, string url, string body)
        {
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url);
            request.Method = method; request.Timeout = 5000; request.ReadWriteTimeout = 5000;
            if (body != null)
            {
                byte[] bytes = Encoding.UTF8.GetBytes(body); request.ContentType = "application/json"; request.ContentLength = bytes.Length;
                using (Stream stream = request.GetRequestStream()) stream.Write(bytes, 0, bytes.Length);
            }
            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
            using (StreamReader reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8)) return reader.ReadToEnd();
        }

        private static string ReadLogSegment(string path, long baseline)
        {
            if (!File.Exists(path)) return String.Empty;
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            {
                stream.Position = Math.Min(baseline, stream.Length);
                using (StreamReader reader = new StreamReader(stream, Encoding.UTF8, true))
                {
                    string text = reader.ReadToEnd();
                    return text.Length > 1048576 ? text.Substring(text.Length - 1048576) : text;
                }
            }
        }

        private static string StageDiagnostic(string staging, TodoTarget todo, string attemptId, EventWriter events, string text)
        {
            string fileName = "fgo-maafgo-" + Guid.NewGuid().ToString("N") + ".txt";
            string path = Path.Combine(staging, fileName);
            File.WriteAllText(path, text, new UTF8Encoding(false));
            FileInfo info = new FileInfo(path); string artifactId = Guid.NewGuid().ToString();
            events.Emit("artifact_staged", new Dictionary<string, object>
            {
                { "todoInstanceId", todo.TodoInstanceId }, { "todoAttemptId", attemptId }, { "artifactId", artifactId },
                { "kind", "tool-log-outcome" }, { "fileName", fileName }, { "mimeType", "text/plain" },
                { "sizeBytes", info.Length }, { "sha256", JsonContract.Sha256File(path) }, { "capturedAt", EventWriter.Timestamp(DateTimeOffset.UtcNow) }
            });
            return artifactId;
        }

        private static string ValidateStaging(ExecuteRequest request)
        {
            string configured = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            if (String.IsNullOrWhiteSpace(configured)) throw new RunnerFailure("artifact_staging_missing", "Artifact staging is unavailable.");
            string root = Path.GetFullPath(configured);
            if (!Path.IsPathRooted(root) || root.StartsWith("\\\\", StringComparison.Ordinal) || !Directory.Exists(root) ||
                JsonContract.IsReparse(root) || new DirectoryInfo(root).Name != request.RunAttemptId)
                throw new RunnerFailure("unsafe_artifact_staging", "Artifact staging failed containment checks.");
            return root;
        }

        private static string ReadLineBounded(TextReader reader, int maximum)
        {
            StringBuilder text = new StringBuilder();
            while (true)
            {
                int next = reader.Read();
                if (next < 0) { if (text.Length == 0) throw new RunnerFailure("missing_input", "One request line is required."); break; }
                if (next == '\n') break; if (next == '\r') continue;
                text.Append((char)next); if (text.Length > maximum) throw new RunnerFailure("input_too_large", "Request is too large.");
            }
            return text.ToString();
        }
    }
}
