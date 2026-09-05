using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;

namespace YeYuGamer.AdapterHost.Tests
{
    internal static class FakeRunner
    {
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
        private static readonly TextReader ProtocolInput = new StreamReader(
            Console.OpenStandardInput(), new UTF8Encoding(false), false);
        private static readonly TextWriter ProtocolOutput = new StreamWriter(
            Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true };

        public static int Main(string[] args)
        {
            Dictionary<string, string> options = new Dictionary<string, string>(StringComparer.Ordinal);
            if (args == null || args.Length != 8) return 64;
            for (int index = 0; index < args.Length; index += 2)
            {
                if (!new HashSet<string>(StringComparer.Ordinal)
                    {
                        "--protocol-version", "--run-id", "--run-attempt-id", "--game-id"
                    }.Contains(args[index])) return 64;
                options[args[index]] = args[index + 1];
            }
            if (options["--protocol-version"] != "1.1") return 64;
            string requestLine = ProtocolInput.ReadLine();
            IDictionary<string, object> request = Json.DeserializeObject(requestLine) as IDictionary<string, object>;
            if (request == null || (string)request["runId"] != options["--run-id"] ||
                (string)request["runAttemptId"] != options["--run-attempt-id"] ||
                (string)request["gameId"] != options["--game-id"]) return 64;
            object[] todoIds = request["executableTodoInstanceIds"] as object[];
            object[] todos = request["todos"] as object[];
            if (todoIds == null || todos == null || todoIds.Length != 1 || todos.Length != 1) return 64;
            string todoId = (string)todoIds[0];
            IDictionary<string, object> todo = todos[0] as IDictionary<string, object>;
            string todoAttemptId = "44444444-4444-4444-8444-444444444444";
            string artifactId = "55555555-5555-4555-8555-555555555555";
            string at = "2099-08-28T10:00:01+08:00";

            string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            if (String.IsNullOrEmpty(staging)) return 65;
            Directory.CreateDirectory(staging);
            if ((string)request["gameId"] == "WW" && request.ContainsKey("accountId")) {
                File.WriteAllText(Path.Combine(staging, "account-scope-received.json"), Json.Serialize(new Dictionary<string, object> {
                    { "accountId", request["accountId"] }, { "accountSnapshot", request["accountSnapshot"] },
                    { "cancelAuthorityPresent", request.ContainsKey("cancelAuthority") }
                }), new UTF8Encoding(false));
            }
            string artifactPath = Path.Combine(staging, "evidence.txt");
            File.WriteAllText(artifactPath, "fresh fake evidence", new UTF8Encoding(false));

            Dictionary<string, object> hello = Base(request, "hello", 0, at);
            hello["packageId"] = "legacy-night-rain-gamer";
            hello["packageVersion"] = "0.1.0";
            hello["packageDigest"] = "sha256:" + new string('c', 64);
            hello["runnerPid"] = Process.GetCurrentProcess().Id;
            hello["acceptedTodoInstanceIds"] = todoIds;
            Emit(hello);

            string outcome = Environment.GetEnvironmentVariable("FAKE_ADAPTER_OUTCOME") ?? String.Empty;
            if (String.Equals(outcome, "hello_only", StringComparison.Ordinal)) return 0;
            if (String.Equals(outcome, "hang_after_hello", StringComparison.Ordinal))
            {
                System.Threading.Thread.Sleep(10000);
                return 0;
            }
            if (String.Equals(outcome, "wait_cancel", StringComparison.Ordinal))
            {
                string controlLine = ProtocolInput.ReadLine();
                IDictionary<string, object> control = Json.DeserializeObject(controlLine ?? String.Empty) as IDictionary<string, object>;
                if (control == null || (string)control["controlType"] != "cancel" ||
                    (string)control["runId"] != (string)request["runId"] ||
                    (string)control["runAttemptId"] != (string)request["runAttemptId"] ||
                    (string)control["fencingToken"] != (string)request["fencingToken"]) return 69;
                Dictionary<string, object> cancelled = Base(request, "run_terminal", 1, at);
                cancelled["status"] = "cancelled";
                cancelled["transportOutcome"] = "cancelled";
                cancelled["attemptedTodoInstanceIds"] = new string[0];
                cancelled["completedTodoInstanceIds"] = new string[0];
                string[] cancelledUnresolved = new string[] { todoId };
                cancelled["unresolvedTodoInstanceIds"] = cancelledUnresolved;
                cancelled["terminalEventDigest"] = TerminalDigest(request, "cancelled",
                    new string[0], new string[0], cancelledUnresolved, 0);
                cancelled["exitCode"] = 0;
                Emit(cancelled);
                return 0;
            }

            Dictionary<string, object> started = Base(request, "todo_attempt_started", 1, at);
            started["todoInstanceId"] = todoId;
            started["todoAttemptId"] = todoAttemptId;
            started["attemptNo"] = 1;
            started["operation"] = (string)todo["operation"];
            Emit(started);

            Dictionary<string, object> artifact = Base(request, "artifact_staged", 2, at);
            artifact["todoInstanceId"] = todoId;
            artifact["todoAttemptId"] = todoAttemptId;
            artifact["artifactId"] = artifactId;
            artifact["kind"] = "game-ui-task-result";
            artifact["fileName"] = "evidence.txt";
            artifact["mimeType"] = "text/plain";
            artifact["sizeBytes"] = new FileInfo(artifactPath).Length;
            artifact["sha256"] = Hash(artifactPath);
            artifact["capturedAt"] = at;
            Emit(artifact);

            Dictionary<string, object> terminal = Base(request, "todo_terminal", 3, at);
            terminal["todoInstanceId"] = todoId;
            terminal["todoAttemptId"] = todoAttemptId;
            bool humanRequired = String.Equals(outcome, "human_required", StringComparison.Ordinal);
            bool failed = String.Equals(outcome, "failed", StringComparison.Ordinal);
            terminal["status"] = failed ? "failed" : (humanRequired ? "human_required" : "completed");
            terminal["reasonCode"] = failed ? "upstream_stage_failed" : (humanRequired ? "login_required" : "fake_evidence_confirmed");
            terminal["reason"] = failed
                ? "upstream task reported a recoverable failure"
                : (humanRequired ? "operator login is required; client preserved" : "今日不适用；伪运行器已生成新鲜证据");
            terminal["retryable"] = failed;
            terminal["evidenceArtifactIds"] = new string[] { artifactId };
            Emit(terminal);

            Dictionary<string, object> runTerminal = Base(request, "run_terminal", 4, at);
            string runStatus = failed ? "failed" : (humanRequired ? "human_required" : "completed");
            string[] attemptedIds = new string[] { todoId };
            string[] completedIds = (humanRequired || failed) ? new string[0] : attemptedIds;
            string[] unresolvedIds = (humanRequired || failed) ? attemptedIds : new string[0];
            runTerminal["status"] = runStatus;
            runTerminal["transportOutcome"] = "clean";
            runTerminal["attemptedTodoInstanceIds"] = attemptedIds;
            runTerminal["completedTodoInstanceIds"] = completedIds;
            runTerminal["unresolvedTodoInstanceIds"] = unresolvedIds;
            runTerminal["terminalEventDigest"] = TerminalDigest(request, runStatus,
                attemptedIds, completedIds, unresolvedIds, 0);
            runTerminal["exitCode"] = 0;
            Emit(runTerminal);
            return 0;
        }

        private static Dictionary<string, object> Base(
            IDictionary<string, object> request,
            string eventType,
            int sequence,
            string at)
        {
            return new Dictionary<string, object>
            {
                { "schemaVersion", 1 },
                { "protocolVersion", "1.1" },
                { "eventType", eventType },
                { "sequence", sequence },
                { "runId", request["runId"] },
                { "runAttemptId", request["runAttemptId"] },
                { "fencingToken", request["fencingToken"] },
                { "gameId", request["gameId"] },
                { "at", at }
            };
        }

        private static void Emit(Dictionary<string, object> value)
        {
            ProtocolOutput.WriteLine(Json.Serialize(value));
        }

        private static string Hash(string path)
        {
            using (FileStream stream = File.OpenRead(path))
            using (SHA256 sha = SHA256.Create())
            {
                byte[] digest = sha.ComputeHash(stream);
                StringBuilder text = new StringBuilder(64);
                foreach (byte item in digest) text.Append(item.ToString("x2"));
                return text.ToString();
            }
        }

        private static string TerminalDigest(IDictionary<string, object> request, string status,
            string[] attempted, string[] completed, string[] unresolved, int exitCode)
        {
            string payload = (string)request["runId"] + "\n" + (string)request["runAttemptId"] + "\n" +
                status + "\n" + String.Join("\n", attempted) + "\n--completed--\n" +
                String.Join("\n", completed) + "\n--unresolved--\n" + String.Join("\n", unresolved) +
                "\n" + exitCode;
            using (SHA256 sha = SHA256.Create())
            {
                byte[] digest = sha.ComputeHash(Encoding.UTF8.GetBytes(payload));
                StringBuilder text = new StringBuilder(71).Append("sha256:");
                foreach (byte item in digest) text.Append(item.ToString("x2"));
                return text.ToString();
            }
        }
    }
}
