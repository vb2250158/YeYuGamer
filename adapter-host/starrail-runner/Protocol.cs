using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace YeYuGamer.StarRailAdapter
{
    internal sealed class RunnerValidationException : Exception
    {
        public string Code { get; private set; }

        public RunnerValidationException(string code, string message) : base(message)
        {
            Code = code;
        }
    }

    internal sealed class RunnerArguments
    {
        public bool ProbeBinding;
        public string ProtocolVersion;
        public string RunId;
        public string RunAttemptId;
        public string GameId;

        public static RunnerArguments Parse(string[] args)
        {
            if (args.Length == 1 && args[0] == "--probe-binding")
                return new RunnerArguments { ProbeBinding = true };
            if (args.Length != 8)
                throw new RunnerValidationException("invalid_arguments", "The fixed execute argument contract is required.");
            Dictionary<string, string> values = new Dictionary<string, string>(StringComparer.Ordinal);
            for (int index = 0; index < args.Length; index += 2)
            {
                if (!args[index].StartsWith("--", StringComparison.Ordinal) || values.ContainsKey(args[index]))
                    throw new RunnerValidationException("invalid_arguments", "Execute arguments are duplicated or malformed.");
                values.Add(args[index], args[index + 1]);
            }
            StrictJson.ExactKeys(values, new string[] { "--protocol-version", "--run-id", "--run-attempt-id", "--game-id" }, "arguments");
            if (values["--protocol-version"] != Protocol.ProtocolVersion)
                throw new RunnerValidationException("protocol_version_mismatch", "Only Adapter protocol v1.1 is supported.");
            StrictJson.CanonicalUuid(values["--run-id"], "runId");
            StrictJson.CanonicalUuid(values["--run-attempt-id"], "runAttemptId");
            if (values["--game-id"] != Protocol.GameId)
                throw new RunnerValidationException("game_scope_mismatch", "Only StarRail is supported by this runner.");
            return new RunnerArguments
            {
                ProtocolVersion = values["--protocol-version"],
                RunId = values["--run-id"],
                RunAttemptId = values["--run-attempt-id"],
                GameId = values["--game-id"]
            };
        }
    }

    internal sealed class TodoTarget
    {
        public string TodoInstanceId;
        public string TodoDefinitionId;
        public int DefinitionVersion;
        public string Operation;
        public string Risk;
        public string CapabilityRef;
        public int PriorAttempts;
    }

    internal sealed class ExecuteRequest
    {
        public string RunId;
        public string RunAttemptId;
        public string FencingToken;
        public string GameId;
        public string Cadence;
        public long ManagerStateVersion;
        public string CatalogVersion;
        public string PolicyDigest;
        public DateTimeOffset IssuedAt;
        public DateTimeOffset ExpiresAt;
        public int TimeoutSeconds;
        public List<string> ExecutableTodoIds;
        public List<TodoTarget> Todos;

        public static ExecuteRequest Parse(string text, RunnerArguments args)
        {
            IDictionary<string, object> value = StrictJson.Object(text, "execute request", 256 * 1024);
            StrictJson.ExactKeys(value, new string[]
            {
                "schemaVersion", "protocolVersion", "requestType", "runId", "runAttemptId",
                "fencingToken", "gameId", "cadence", "managerStateVersion", "catalogVersion",
                "policyDigest", "issuedAt", "expiresAt", "timeoutSeconds", "preserveClientOnStop",
                "executableTodoInstanceIds", "todos"
            }, "execute request");
            if (StrictJson.Integer(value["schemaVersion"], "schemaVersion", 1, 1) != 1 ||
                StrictJson.Text(value["protocolVersion"], "protocolVersion", 8) != Protocol.ProtocolVersion ||
                StrictJson.Text(value["requestType"], "requestType", 16) != "execute")
                throw new RunnerValidationException("unsupported_request", "The execute schema or protocol is unsupported.");
            string runId = StrictJson.CanonicalUuid(value["runId"], "runId");
            string runAttemptId = StrictJson.CanonicalUuid(value["runAttemptId"], "runAttemptId");
            string gameId = StrictJson.Text(value["gameId"], "gameId", 32);
            if (runId != args.RunId || runAttemptId != args.RunAttemptId || gameId != args.GameId || gameId != Protocol.GameId)
                throw new RunnerValidationException("request_scope_mismatch", "The request differs from the fixed Host scope.");
            string cadence = StrictJson.Text(value["cadence"], "cadence", 16);
            if (cadence != "daily")
                throw new RunnerValidationException("cadence_not_supported", "The StarRail candidate supports daily cadence only.");
            if (!(value["preserveClientOnStop"] is bool) || !(bool)value["preserveClientOnStop"])
                throw new RunnerValidationException("unsafe_stop_policy", "preserveClientOnStop must be true.");
            string token = StrictJson.Text(value["fencingToken"], "fencingToken", 256);
            if (!Regex.IsMatch(token, "^[A-Za-z0-9._~-]{32,256}$", RegexOptions.CultureInvariant))
                throw new RunnerValidationException("invalid_fencing_token", "The fencing token is malformed.");
            string policyDigest = StrictJson.Text(value["policyDigest"], "policyDigest", 71);
            if (!Regex.IsMatch(policyDigest, "^(sha256:)?[0-9a-f]{64}$", RegexOptions.CultureInvariant))
                throw new RunnerValidationException("invalid_policy_digest", "The policy digest is malformed.");
            DateTimeOffset issuedAt = StrictJson.Time(value["issuedAt"], "issuedAt");
            DateTimeOffset expiresAt = StrictJson.Time(value["expiresAt"], "expiresAt");
            if (expiresAt <= issuedAt)
                throw new RunnerValidationException("invalid_lease", "The execution lease is inverted.");
            DateTimeOffset now = DateTimeOffset.UtcNow;
            if (now < issuedAt.AddSeconds(-5) || now >= expiresAt)
                throw new RunnerValidationException("stale_lease", "The execution lease is not current.");
            int timeout = checked((int)StrictJson.Integer(value["timeoutSeconds"], "timeoutSeconds", 1, 86400));
            if (timeout > (expiresAt - issuedAt).TotalSeconds)
                throw new RunnerValidationException("invalid_lease", "The timeout exceeds the execution lease.");

            List<object> idValues = StrictJson.Array(value["executableTodoInstanceIds"], "executableTodoInstanceIds", 1, 64);
            List<string> ids = idValues.Select(delegate(object item) { return StrictJson.TodoInstanceId(item, "executableTodoInstanceIds"); }).ToList();
            if (ids.Distinct(StringComparer.Ordinal).Count() != ids.Count)
                throw new RunnerValidationException("duplicate_todo", "Executable Todo IDs must be unique.");
            List<object> todoValues = StrictJson.Array(value["todos"], "todos", 1, 64);
            List<TodoTarget> todos = new List<TodoTarget>();
            foreach (object item in todoValues)
                todos.Add(ParseTodo(item));
            if (!ids.SequenceEqual(todos.Select(delegate(TodoTarget item) { return item.TodoInstanceId; }), StringComparer.Ordinal))
                throw new RunnerValidationException("todo_scope_mismatch", "Todo documents must match executable IDs in order.");
            return new ExecuteRequest
            {
                RunId = runId,
                RunAttemptId = runAttemptId,
                FencingToken = token,
                GameId = gameId,
                Cadence = cadence,
                ManagerStateVersion = StrictJson.Integer(value["managerStateVersion"], "managerStateVersion", 0, Int64.MaxValue),
                CatalogVersion = StrictJson.Identifier(value["catalogVersion"], "catalogVersion"),
                PolicyDigest = policyDigest,
                IssuedAt = issuedAt,
                ExpiresAt = expiresAt,
                TimeoutSeconds = timeout,
                ExecutableTodoIds = ids,
                Todos = todos
            };
        }

        private static TodoTarget ParseTodo(object document)
        {
            IDictionary<string, object> value = StrictJson.AsObject(document, "Todo target");
            StrictJson.ExactKeys(value, new string[]
            {
                "todoInstanceId", "todoDefinitionId", "definitionVersion", "operation", "risk",
                "adapterCapabilityRef", "priorAttempts", "executionDisposition"
            }, "Todo target");
            string operation = StrictJson.Identifier(value["operation"], "operation");
            string definitionId = StrictJson.Identifier(value["todoDefinitionId"], "todoDefinitionId");
            Protocol.OperationDefinition expected;
            if (!Protocol.Operations.TryGetValue(operation, out expected) || expected.TodoDefinitionId != definitionId)
                throw new RunnerValidationException("operation_not_bound", "The Todo operation is not in the StarRail binding.");
            if (StrictJson.Text(value["risk"], "risk", 32) != expected.Risk ||
                StrictJson.Text(value["adapterCapabilityRef"], "adapterCapabilityRef", 128) != Protocol.CapabilityRef ||
                StrictJson.Text(value["executionDisposition"], "executionDisposition", 32) != "executable")
                throw new RunnerValidationException("todo_not_executable", "The Todo risk, capability or disposition is not executable.");
            return new TodoTarget
            {
                TodoInstanceId = StrictJson.TodoInstanceId(value["todoInstanceId"], "todoInstanceId"),
                TodoDefinitionId = definitionId,
                DefinitionVersion = checked((int)StrictJson.Integer(value["definitionVersion"], "definitionVersion", 1, Int32.MaxValue)),
                Operation = operation,
                Risk = expected.Risk,
                CapabilityRef = Protocol.CapabilityRef,
                PriorAttempts = checked((int)StrictJson.Integer(value["priorAttempts"], "priorAttempts", 0, Int32.MaxValue))
            };
        }
    }

    internal sealed class PackageIdentity
    {
        public string Version;
        public string Digest;

        public static PackageIdentity Load(string root)
        {
            string manifestPath = Path.Combine(root, "install-manifest.json");
            if (!File.Exists(manifestPath) || StrictJson.IsReparse(manifestPath))
                throw new RunnerValidationException("package_manifest_missing", "The package manifest is missing or unsafe.");
            byte[] raw = File.ReadAllBytes(manifestPath);
            IDictionary<string, object> value = StrictJson.Object(StrictJson.Utf8(raw, "package manifest"), "package manifest", 64 * 1024);
            if (StrictJson.Text(value.ContainsKey("packageId") ? value["packageId"] : null, "packageId", 128) != Protocol.PackageId)
                throw new RunnerValidationException("package_scope_mismatch", "The package ID is invalid.");
            string version = StrictJson.Identifier(value.ContainsKey("packageVersion") ? value["packageVersion"] : null, "packageVersion");
            List<object> files = StrictJson.Array(value.ContainsKey("files") ? value["files"] : null, "files", 1, 128);
            Dictionary<string, string> hashes = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (object document in files)
            {
                IDictionary<string, object> item = StrictJson.AsObject(document, "execution file");
                StrictJson.ExactKeys(item, new string[] { "path", "sha256", "sizeBytes" }, "execution file");
                string relative = StrictJson.RelativePath(item["path"], "path");
                if (relative == "install-manifest.json" || hashes.ContainsKey(relative))
                    throw new RunnerValidationException("package_file_invalid", "The package file list is duplicated.");
                string digest = StrictJson.Text(item["sha256"], "sha256", 64);
                if (!Regex.IsMatch(digest, "^[0-9a-f]{64}$", RegexOptions.CultureInvariant))
                    throw new RunnerValidationException("package_file_invalid", "A package file digest is invalid.");
                string full = StrictJson.ContainedFile(root, relative);
                long size = StrictJson.Integer(item["sizeBytes"], "sizeBytes", 1, Int64.MaxValue);
                FileInfo info = new FileInfo(full);
                if (!info.Exists || info.Length != size || StrictJson.IsReparse(full) || StrictJson.Sha256File(full) != digest)
                    throw new RunnerValidationException("package_file_mismatch", "A package file differs from its manifest.");
                hashes.Add(relative, digest);
            }
            HashSet<string> actual = new HashSet<string>(
                Directory.GetFiles(root, "*", SearchOption.AllDirectories).Select(delegate(string path)
                {
                    return path.Substring(root.TrimEnd(Path.DirectorySeparatorChar).Length + 1).Replace('\\', '/');
                }), StringComparer.Ordinal);
            HashSet<string> expected = new HashSet<string>(hashes.Keys, StringComparer.Ordinal);
            expected.Add("install-manifest.json");
            if (!actual.SetEquals(expected))
                throw new RunnerValidationException("package_file_extras", "The package has missing or unexpected files.");
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
                return new PackageIdentity { Version = version, Digest = StrictJson.Hex(sha.ComputeHash(stream)) };
            }
        }
    }

    internal sealed class CommandBinding
    {
        public string Task;
        public int TimeoutSeconds;
    }

    internal sealed class ToolBinding
    {
        public string Root;
        public string FormalLauncherPath;
        public string CommandPath;
        public string GamePath;
        public string ConfigPath;
        public Dictionary<string, CommandBinding> Commands;

        public static ToolBinding Load(string packageRoot)
        {
            string path = Path.Combine(packageRoot, "tool-binding.json");
            IDictionary<string, object> value = StrictJson.Object(File.ReadAllText(path, Encoding.UTF8), "tool binding", 64 * 1024);
            StrictJson.ExactKeys(value, new string[] { "schemaVersion", "bindingId", "gameId", "tool", "commands", "safety" }, "tool binding");
            if (StrictJson.Integer(value["schemaVersion"], "schemaVersion", 1, 1) != 1 ||
                StrictJson.Text(value["bindingId"], "bindingId", 128) != "starrail-march7th-v1" ||
                StrictJson.Text(value["gameId"], "gameId", 32) != Protocol.GameId)
                throw new RunnerValidationException("binding_scope_mismatch", "The tool binding scope is invalid.");
            IDictionary<string, object> tool = StrictJson.AsObject(value["tool"], "tool");
            StrictJson.ExactKeys(tool, new string[] { "root", "formalLauncher", "formalLauncherSha256", "command", "commandSha256", "config", "configSha256" }, "tool");
            string root = Path.GetFullPath(StrictJson.Text(tool["root"], "root", 1024));
            if (!Path.IsPathRooted(root) || root.StartsWith("\\\\", StringComparison.Ordinal) || !Directory.Exists(root) || StrictJson.IsReparse(root))
                throw new RunnerValidationException("unsafe_tool_root", "The tool root must be a local non-reparse directory.");
            string formalLauncherLeaf = StrictJson.Leaf(tool["formalLauncher"], "formalLauncher");
            string commandLeaf = StrictJson.Leaf(tool["command"], "command");
            string configLeaf = StrictJson.Leaf(tool["config"], "config");
            string formalLauncherPath = StrictJson.ContainedFile(root, formalLauncherLeaf);
            string commandPath = StrictJson.ContainedFile(root, commandLeaf);
            string configPath = StrictJson.ContainedFile(root, configLeaf);
            string formalLauncherHash = StrictJson.Text(tool["formalLauncherSha256"], "formalLauncherSha256", 64);
            string commandHash = StrictJson.Text(tool["commandSha256"], "commandSha256", 64);
            StrictJson.Text(tool["configSha256"], "configSha256", 64);
            // March7th Assistant legitimately rewrites config.yaml when its
            // formal GUI saves state or completes an update.  Executable drift
            // remains a hard failure, while the mutable configuration is
            // checked semantically below against the fixed safety contract.
            if (StrictJson.Sha256File(formalLauncherPath) != formalLauncherHash || StrictJson.Sha256File(commandPath) != commandHash)
                throw new RunnerValidationException("tool_binding_drift", "The bound launcher or command changed after promotion.");
            IDictionary<string, object> commands = StrictJson.AsObject(value["commands"], "commands");
            StrictJson.ExactKeys(commands, Protocol.March7thOperations.Keys.ToArray(), "commands");
            Dictionary<string, CommandBinding> parsedCommands = new Dictionary<string, CommandBinding>(StringComparer.Ordinal);
            foreach (KeyValuePair<string, Protocol.OperationDefinition> operation in Protocol.March7thOperations)
            {
                IDictionary<string, object> command = StrictJson.AsObject(commands[operation.Key], "command");
                StrictJson.ExactKeys(command, new string[] { "task", "timeoutSeconds" }, "command");
                string task = StrictJson.Identifier(command["task"], "task");
                if (task != operation.Value.Task)
                    throw new RunnerValidationException("command_binding_mismatch", "A tool task differs from the fixed operation map.");
                parsedCommands.Add(operation.Key, new CommandBinding
                {
                    Task = task,
                    TimeoutSeconds = checked((int)StrictJson.Integer(command["timeoutSeconds"], "timeoutSeconds", 1, 3600))
                });
            }
            IDictionary<string, object> safety = StrictJson.AsObject(value["safety"], "safety");
            StrictJson.ExactKeys(safety, new string[]
            {
                "requiredInstanceType", "useReservedTrailblazePower", "afterFinish",
                "autoSetGamePath", "preserveGameClient"
            }, "safety");
            if (StrictJson.Text(safety["requiredInstanceType"], "requiredInstanceType", 64) != "拟造花萼（金）" ||
                !(safety["useReservedTrailblazePower"] is bool) || (bool)safety["useReservedTrailblazePower"] ||
                StrictJson.Text(safety["afterFinish"], "afterFinish", 32) != "None" ||
                !(safety["autoSetGamePath"] is bool) || (bool)safety["autoSetGamePath"] ||
                !(safety["preserveGameClient"] is bool) || !(bool)safety["preserveGameClient"])
                throw new RunnerValidationException("unsafe_binding_policy", "The StarRail safety policy differs from the fixed contract.");
            ValidateConfig(configPath);
            string gamePath = ReadGamePath(configPath);
            return new ToolBinding { Root = root, FormalLauncherPath = formalLauncherPath, CommandPath = commandPath, GamePath = gamePath, ConfigPath = configPath, Commands = parsedCommands };
        }

        private static string ReadGamePath(string configPath)
        {
            string text = File.ReadAllText(configPath, Encoding.UTF8);
            Match match = Regex.Match(text, @"(?m)^game_path:\s*([^#\r\n]*?)\s*(?:#.*)?$");
            if (!match.Success)
                throw new RunnerValidationException("unsafe_game_path", "March7th config has no fixed game_path.");
            string path = Path.GetFullPath(match.Groups[1].Value.Trim().Trim('\'', '"'));
            if (!Path.IsPathRooted(path) || path.StartsWith("\\\\", StringComparison.Ordinal) ||
                !File.Exists(path) || StrictJson.IsReparse(path) ||
                !String.Equals(Path.GetFileName(path), "StarRail.exe", StringComparison.OrdinalIgnoreCase))
                throw new RunnerValidationException("unsafe_game_path", "March7th game_path must be a local StarRail.exe file.");
            return path;
        }

        private static void ValidateConfig(string configPath)
        {
            string text = File.ReadAllText(configPath, Encoding.UTF8);
            Dictionary<string, string> required = new Dictionary<string, string>(StringComparer.Ordinal)
            {
                { "instance_type", "拟造花萼（金）" },
                { "use_reserved_trailblaze_power", "false" },
                { "after_finish", "None" },
                { "auto_set_game_path_enable", "false" },
                { "exit_after_failure", "false" },
                { "scheduled_run_enable", "false" }
            };
            foreach (KeyValuePair<string, string> item in required)
            {
                Match match = Regex.Match(text, "(?m)^" + Regex.Escape(item.Key) + @":\s*([^#\r\n]*?)\s*(?:#.*)?$");
                if (!match.Success || match.Groups[1].Value.Trim() != item.Value)
                    throw new RunnerValidationException("unsafe_tool_config", "A required StarRail safety setting is absent or changed: " + item.Key);
            }
            Match scheduled = Regex.Match(text, @"(?m)^scheduled_tasks:\s*([^#\r\n]*?)\s*(?:#.*)?$");
            if (!scheduled.Success || scheduled.Groups[1].Value.Trim() != "[]")
                throw new RunnerValidationException("unsafe_tool_config", "March7th scheduled tasks must be empty.");
            Match gamePath = Regex.Match(text, @"(?m)^game_path:\s*([^#\r\n]*?)\s*(?:#.*)?$");
            if (!gamePath.Success)
                throw new RunnerValidationException("unsafe_tool_config", "The fixed game path is missing.");
            string value = gamePath.Groups[1].Value.Trim().Trim('\'', '"');
            if (!Path.IsPathRooted(value) || value.StartsWith("\\\\", StringComparison.Ordinal) || !File.Exists(value))
                throw new RunnerValidationException("unsafe_tool_config", "The game executable path is not a local file.");
        }
    }

    internal static class Protocol
    {
        public const string ProtocolVersion = "1.1";
        public const string PackageId = "legacy-night-rain-gamer";
        public const string GameId = "StarRail";
        public const string CapabilityRef = "game.daily.run@1.0";

        internal sealed class OperationDefinition
        {
            public string TodoDefinitionId;
            public string Task;
            public string Risk;

            public bool UsesMarch7th
            {
                get { return !String.IsNullOrEmpty(Task); }
            }
        }

        public static readonly Dictionary<string, OperationDefinition> Operations =
            new Dictionary<string, OperationDefinition>(StringComparer.Ordinal)
            {
                { "attach-home", new OperationDefinition { TodoDefinitionId = "todo.v1.starrail.daily.attach-home", Task = "game", Risk = "routine_action" } },
                { "spend-trailblaze-power", new OperationDefinition { TodoDefinitionId = "todo.v1.starrail.daily.spend-trailblaze-power", Task = "power", Risk = "routine_action" } },
                { "daily-training-objectives", new OperationDefinition { TodoDefinitionId = "todo.v1.starrail.daily.daily-training-objectives", Task = "daily", Risk = "routine_action" } },
                { "claim-daily-training-rewards", new OperationDefinition { TodoDefinitionId = "todo.v1.starrail.daily.claim-daily-training-rewards", Task = "daily", Risk = "routine_action" } },
                { "verify-daily-task-list", new OperationDefinition { TodoDefinitionId = "todo.v1.starrail.daily.verify-daily-task-list", Task = null, Risk = "observe_only" } }
            };

        public static readonly Dictionary<string, OperationDefinition> March7thOperations =
            Operations.Where(delegate(KeyValuePair<string, OperationDefinition> item) { return item.Value.UsesMarch7th; })
                .ToDictionary(delegate(KeyValuePair<string, OperationDefinition> item) { return item.Key; },
                    delegate(KeyValuePair<string, OperationDefinition> item) { return item.Value; }, StringComparer.Ordinal);

        public static bool UsesMarch7th(string operation)
        {
            OperationDefinition definition;
            return Operations.TryGetValue(operation, out definition) && definition.UsesMarch7th;
        }
    }

    internal sealed class EventWriter
    {
        private readonly ExecuteRequest request;
        private readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 256 * 1024 };
        private readonly MemoryStream transcript = new MemoryStream();
        private long sequence;

        public EventWriter(ExecuteRequest request)
        {
            this.request = request;
        }

        public void Emit(string eventType, IDictionary<string, object> fields)
        {
            Dictionary<string, object> document = new Dictionary<string, object>(StringComparer.Ordinal)
            {
                { "schemaVersion", 1 }, { "protocolVersion", Protocol.ProtocolVersion },
                { "eventType", eventType }, { "sequence", sequence++ },
                { "runId", request.RunId }, { "runAttemptId", request.RunAttemptId },
                { "fencingToken", request.FencingToken }, { "gameId", request.GameId },
                { "at", Timestamp(DateTimeOffset.UtcNow) }
            };
            foreach (KeyValuePair<string, object> item in fields)
                document.Add(item.Key, item.Value);
            string line = json.Serialize(document);
            byte[] encoded = Encoding.UTF8.GetBytes(line + "\n");
            transcript.Write(encoded, 0, encoded.Length);
            Console.Out.WriteLine(line);
            Console.Out.Flush();
        }

        public string Digest()
        {
            using (SHA256 sha = SHA256.Create())
                return "sha256:" + StrictJson.Hex(sha.ComputeHash(transcript.ToArray()));
        }

        public string TerminalDigest(string status, IEnumerable<string> attempted,
            IEnumerable<string> completed, IEnumerable<string> unresolved, int exitCode)
        {
            string payload = request.RunId + "\n" + request.RunAttemptId + "\n" + status + "\n" +
                String.Join("\n", attempted) + "\n--completed--\n" + String.Join("\n", completed) +
                "\n--unresolved--\n" + String.Join("\n", unresolved) + "\n" +
                exitCode.ToString(CultureInfo.InvariantCulture);
            using (SHA256 sha = SHA256.Create())
                return "sha256:" + StrictJson.Hex(sha.ComputeHash(Encoding.UTF8.GetBytes(payload)));
        }

        public static string Timestamp(DateTimeOffset value)
        {
            return value.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture);
        }
    }

    internal static class StrictJson
    {
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 256 * 1024 };
        private static readonly Regex IdentifierPattern = new Regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", RegexOptions.CultureInvariant);
        private static readonly Regex TodoPattern = new Regex("^todo-instance-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", RegexOptions.CultureInvariant);

        public static IDictionary<string, object> Object(string text, string context, int maximum)
        {
            if (String.IsNullOrEmpty(text) || Encoding.UTF8.GetByteCount(text) > maximum)
                throw new RunnerValidationException("invalid_json", context + " has an invalid size.");
            object value;
            try { value = Json.DeserializeObject(text); }
            catch { throw new RunnerValidationException("invalid_json", context + " is not one JSON object."); }
            return AsObject(value, context);
        }

        public static IDictionary<string, object> AsObject(object value, string context)
        {
            IDictionary<string, object> result = value as IDictionary<string, object>;
            if (result == null)
                throw new RunnerValidationException("invalid_schema", context + " must be an object.");
            return result;
        }

        public static void ExactKeys<T>(IDictionary<string, T> value, IEnumerable<string> required, string context)
        {
            HashSet<string> expected = new HashSet<string>(required, StringComparer.Ordinal);
            if (!expected.SetEquals(value.Keys))
                throw new RunnerValidationException("invalid_schema", context + " fields differ from the fixed contract.");
        }

        public static string Text(object value, string field, int maximum)
        {
            string text = value as string;
            if (String.IsNullOrEmpty(text) || text.Length > maximum || text.Any(delegate(char character) { return character < 32; }))
                throw new RunnerValidationException("invalid_schema", field + " is invalid.");
            return text;
        }

        public static string Identifier(object value, string field)
        {
            string text = Text(value, field, 128);
            if (!IdentifierPattern.IsMatch(text))
                throw new RunnerValidationException("invalid_schema", field + " is not an identifier.");
            return text;
        }

        public static string CanonicalUuid(object value, string field)
        {
            string text = Text(value, field, 36);
            Guid parsed;
            if (!Guid.TryParse(text, out parsed) || parsed.ToString() != text)
                throw new RunnerValidationException("invalid_scope", field + " must be a lowercase canonical UUID.");
            return text;
        }

        public static string TodoInstanceId(object value, string field)
        {
            string text = Text(value, field, 50);
            Match match = TodoPattern.Match(text);
            if (!match.Success)
                throw new RunnerValidationException("invalid_scope", field + " is not a Todo instance ID.");
            CanonicalUuid(match.Groups[1].Value, field);
            return text;
        }

        public static long Integer(object value, string field, long minimum, long maximum)
        {
            if (value is bool || value == null)
                throw new RunnerValidationException("invalid_schema", field + " is not an integer.");
            long result;
            try { result = Convert.ToInt64(value, CultureInfo.InvariantCulture); }
            catch { throw new RunnerValidationException("invalid_schema", field + " is not an integer."); }
            if (result < minimum || result > maximum || (value is double && (double)value != result) || (value is decimal && (decimal)value != result))
                throw new RunnerValidationException("invalid_schema", field + " is outside its bounds.");
            return result;
        }

        public static DateTimeOffset Time(object value, string field)
        {
            string text = Text(value, field, 64);
            DateTimeOffset parsed;
            if (!DateTimeOffset.TryParse(text, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out parsed) ||
                !(text.EndsWith("Z", StringComparison.Ordinal) || Regex.IsMatch(text, "[+-][0-9]{2}:[0-9]{2}$")))
                throw new RunnerValidationException("invalid_schema", field + " is not an offset ISO timestamp.");
            return parsed;
        }

        public static List<object> Array(object value, string field, int minimum, int maximum)
        {
            IEnumerable source = value as IEnumerable;
            if (source == null || value is string || value is IDictionary)
                throw new RunnerValidationException("invalid_schema", field + " must be an array.");
            List<object> result = new List<object>();
            foreach (object item in source) result.Add(item);
            if (result.Count < minimum || result.Count > maximum)
                throw new RunnerValidationException("invalid_schema", field + " has an invalid length.");
            return result;
        }

        public static string Leaf(object value, string field)
        {
            string text = Text(value, field, 128);
            if (Path.GetFileName(text) != text || text == "." || text == ".." || text.IndexOfAny(new char[] { '/', '\\' }) >= 0)
                throw new RunnerValidationException("unsafe_path", field + " must be a leaf name.");
            return text;
        }

        public static string RelativePath(object value, string field)
        {
            string text = Text(value, field, 256).Replace('\\', '/');
            if (Path.IsPathRooted(text) || text.StartsWith("/", StringComparison.Ordinal) || text.Split('/').Any(delegate(string part) { return part.Length == 0 || part == "." || part == ".."; }))
                throw new RunnerValidationException("unsafe_path", field + " is not a safe relative path.");
            return text;
        }

        public static string ContainedFile(string root, string relative)
        {
            string canonicalRoot = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            string path = Path.GetFullPath(Path.Combine(canonicalRoot, relative.Replace('/', Path.DirectorySeparatorChar)));
            if (!path.StartsWith(canonicalRoot, StringComparison.OrdinalIgnoreCase) || !File.Exists(path) || IsReparse(path))
                throw new RunnerValidationException("unsafe_path", "A bound file is missing or outside its root.");
            return path;
        }

        public static bool IsReparse(string path)
        {
            return (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0;
        }

        public static string Sha256File(string path)
        {
            using (SHA256 sha = SHA256.Create())
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                return Hex(sha.ComputeHash(stream));
        }

        public static string Hex(byte[] bytes)
        {
            return BitConverter.ToString(bytes).Replace("-", String.Empty).ToLowerInvariant();
        }

        public static string Utf8(byte[] bytes, string context)
        {
            try { return new UTF8Encoding(false, true).GetString(bytes); }
            catch { throw new RunnerValidationException("invalid_utf8", context + " is not UTF-8."); }
        }
    }
}
