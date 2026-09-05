using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace YeYuGamer.AdapterHost
{
    internal static class Program
    {
        private const string ProtocolVersion = "1.1";
        private const string HostVersion = "0.3.6";
        private const string ExecutionPackageId = "legacy-night-rain-gamer";
        private const int MaxRequestBytes = 262144;
        private const int MaxEventBytes = 65536;
        private const int MaxManifestBytes = 65536;
        private const int MaxStderrBytes = 8192;
        private const int CancelGraceSeconds = 5;
        private const uint JobObjectLimitKillOnJobClose = 0x00002000;

        private static readonly HashSet<string> AllowedGameIds = new HashSet<string>(StringComparer.Ordinal)
        {
            "WW", "PGR", "StarRail", "ZZZ", "Endfield", "GF2",
            "NTE", "FGO", "NIKKE", "BD2", "CZN"
        };
        private static readonly HashSet<string> AllowedEventTypes = new HashSet<string>(StringComparer.Ordinal)
        {
            "hello", "todo_attempt_started", "todo_progress", "artifact_staged",
            "todo_terminal", "run_terminal"
        };
        private static readonly HashSet<string> AllowedTodoTerminalStatuses = new HashSet<string>(StringComparer.Ordinal)
        {
            // A Runner must be able to report an upstream failure as a normal
            // Todo terminal fact.  Rejecting it leaves the Host waiting for a
            // terminal event that it has already received, so a failed tool
            // appears as a permanently-starting batch in WebGUI.
            "completed", "failed", "skipped", "blocked", "review_required", "human_required"
        };
        private static readonly HashSet<string> AllowedRunTerminalStatuses = new HashSet<string>(StringComparer.Ordinal)
        {
            "completed", "partial", "blocked", "review_required", "human_required",
            "cancelled", "failed"
        };
        private static readonly HashSet<string> AllowedTransportOutcomes = new HashSet<string>(StringComparer.Ordinal)
        {
            "clean", "crashed", "timeout", "cancelled"
        };
        private static readonly HashSet<string> RequiredForbiddenClasses = new HashSet<string>(StringComparer.Ordinal)
        {
            "gacha", "purchase", "dismantle", "enhance", "trade", "account_settings",
            "pvp", "irreversible_choice", "arbitrary_command", "arbitrary_path", "arbitrary_input"
        };
        private static readonly Regex Identifier = new Regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$");
        private static readonly Regex Capability = new Regex("^[a-z0-9][a-z0-9._:-]{0,95}@[1-9][0-9]*\\.[0-9]+$");
        private static readonly Regex TodoId = new Regex("^todo-instance-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$");
        private static readonly Regex Token = new Regex("^[A-Za-z0-9._~-]{32,256}$");
        private static readonly Regex Digest = new Regex("^(sha256:)?[0-9a-f]{64}$");
        private static readonly Regex AuthorityMac = new Regex("^hmac-sha256:[0-9a-f]{64}$");
        private static readonly Regex IsoTimestampWithOffset = new Regex(
            "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d{1,7})?(?:Z|[+-]\\d{2}:\\d{2})$");
        private static readonly object RunnerInputGate = new object();
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = MaxRequestBytes };
        private static readonly TextReader ProtocolInput = new StreamReader(
            Console.OpenStandardInput(), new UTF8Encoding(false), false);
        private static readonly TextWriter ProtocolOutput = new StreamWriter(
            Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true };

        public static int Main(string[] args)
        {
            // Protocol 1.1 is UTF-8 JSONL.  When stdout is redirected on a
            // Chinese Windows installation, .NET Framework otherwise uses the
            // active OEM code page.  ASCII-only events happen to survive, but
            // the first localized reason becomes invalid UTF-8 at Manager.
            ParsedArguments parsed;
            string error;
            try
            {
                if (!TryParseArguments(args, out parsed, out error))
                {
                    WriteResult(false, "invalid_request", parsed, error);
                    return 64;
                }
                if (parsed.Operation == "probe")
                {
                    WriteResult(true, "host_ready", parsed, "Adapter Host protocol v1.1 is ready; execution requires a promoted v2 package.");
                    return 0;
                }
                if (parsed.Operation == "canary")
                {
                    WriteResult(true, "canary_passed", parsed, "No-input canary passed. No Adapter or game process was started.");
                    return 0;
                }
                return Execute(parsed);
            }
            catch (Exception failure)
            {
                WriteResult(false, "host_error", new ParsedArguments(), failure.GetType().Name);
                return 70;
            }
        }

        private static int Execute(ParsedArguments parsed)
        {
            string requestJson;
            bool eof;
            if (!ReadLineBounded(ProtocolInput, MaxRequestBytes, out requestJson, out eof))
            {
                WriteResult(false, eof ? "missing_execute_request" : "execute_request_too_large", parsed, "Execute requires one bounded stdin JSON request.");
                return 64;
            }
            ExecuteRequest request;
            string error;
            if (!TryParseRequest(requestJson, parsed, out request, out error))
            {
                WriteResult(false, "invalid_execute_request", parsed, error);
                return 64;
            }
            ExecutionPackage package;
            if (!TryLoadPackage(request, out package, out error))
            {
                WriteResult(false, "execution_package_unavailable", parsed, error);
                return 78;
            }

            Process runner = new Process();
            runner.StartInfo = new ProcessStartInfo
            {
                FileName = package.EntryPoint,
                Arguments = "--protocol-version " + ProtocolVersion + " --run-id " + request.RunId +
                    " --run-attempt-id " + request.RunAttemptId + " --game-id " + request.GameId,
                WorkingDirectory = package.Root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = new UTF8Encoding(false),
                StandardErrorEncoding = new UTF8Encoding(false)
            };
            int stderrBytes = 0;
            int stderrOverflow = 0;
            IntPtr runnerJob = IntPtr.Zero;
            runner.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs eventArgs)
            {
                if (eventArgs.Data != null && Interlocked.Add(ref stderrBytes, Encoding.UTF8.GetByteCount(eventArgs.Data) + 1) > MaxStderrBytes)
                    Interlocked.Exchange(ref stderrOverflow, 1);
            };
            try
            {
                if (!runner.Start()) return 70;
                runnerJob = CreateKillOnCloseJob();
                if (runnerJob == IntPtr.Zero || !AssignProcessToJobObject(runnerJob, runner.Handle))
                    throw new InvalidOperationException("runner_job_assignment_failed");
                runner.BeginErrorReadLine();
                if (!WriteRunnerInput(runner, request.RunnerRequestJson))
                    throw new IOException("runner_request_delivery_failed");
            }
            catch
            {
                try { if (!runner.HasExited) runner.Kill(); } catch { }
                if (runnerJob != IntPtr.Zero) CloseHandle(runnerJob);
                runner.Dispose();
                return 70;
            }

            ControlState controlState = new ControlState();
            Thread control = new Thread(delegate() { RelayControls(request, runner, controlState); });
            control.IsBackground = true;
            control.Name = "YeYuGamerAdapterControl";
            control.Start();
            Thread durableControl = new Thread(delegate() { RelayDurableCancel(request, runner, controlState); });
            durableControl.IsBackground = true;
            durableControl.Name = "YeYuGamerAdapterDurableControl";
            durableControl.Start();

            int nextSequence = 0;
            bool helloSeen = false;
            bool terminalSeen = false;
            int declaredExit = Int32.MinValue;
            bool valid = true;
            string forcedTerminalReason = null;
            var startedTodoIds = new HashSet<string>(StringComparer.Ordinal);
            var completedTodoIds = new HashSet<string>(StringComparer.Ordinal);
            Task<string> pendingRead = null;
            try
            {
                while (true)
                {
                    if (Interlocked.CompareExchange(ref controlState.Failed, 0, 0) != 0)
                    {
                        forcedTerminalReason = "adapter_control_invalid";
                        break;
                    }
                    long cancelDeadlineTicks = Interlocked.Read(ref controlState.CancelDeadlineUtcTicks);
                    if (cancelDeadlineTicks > 0 && DateTimeOffset.UtcNow.UtcDateTime.Ticks >= cancelDeadlineTicks)
                    {
                        forcedTerminalReason = "runner_cancel_grace_exceeded";
                        break;
                    }
                    TimeSpan remaining = request.ExecutionDeadline - DateTimeOffset.UtcNow;
                    if (remaining <= TimeSpan.Zero)
                    {
                        forcedTerminalReason = "runner_deadline_exceeded";
                        break;
                    }
                    if (pendingRead == null) pendingRead = runner.StandardOutput.ReadLineAsync();
                    int waitMilliseconds = (int)Math.Min(1000, Math.Max(1, remaining.TotalMilliseconds));
                    if (!pendingRead.Wait(waitMilliseconds)) continue;
                    string line = pendingRead.Result;
                    pendingRead = null;
                    if (line == null)
                    {
                        if (!runner.HasExited) forcedTerminalReason = "runner_output_closed_without_terminal";
                        break;
                    }
                    if (Encoding.UTF8.GetByteCount(line) > MaxEventBytes ||
                        !ValidateRunnerEvent(line, request, ref nextSequence, ref helloSeen, ref terminalSeen,
                            ref declaredExit, startedTodoIds, completedTodoIds))
                    {
                        valid = false;
                        break;
                    }
                    ProtocolOutput.WriteLine(line);
                }
                if ((!valid || forcedTerminalReason != null) && !runner.HasExited) runner.Kill();
                runner.WaitForExit();
                runner.WaitForExit();
            }
            catch
            {
                try { if (!runner.HasExited) runner.Kill(); } catch { }
                try { runner.WaitForExit(); } catch { }
                valid = false;
            }
            bool synthesizeTerminal = valid && helloSeen && !terminalSeen;
            bool synthesizeCancellation = synthesizeTerminal &&
                Interlocked.CompareExchange(ref controlState.CancelRequested, 0, 0) != 0;
            if (synthesizeTerminal)
            {
                if (String.IsNullOrEmpty(forcedTerminalReason))
                    forcedTerminalReason = runner.HasExited
                        ? "runner_exited_without_terminal"
                        : "runner_output_closed_without_terminal";
                try { if (!runner.HasExited) runner.Kill(); } catch { }
                try { runner.WaitForExit(); } catch { }
                string[] attempted = request.ExecutableTodoIds.Where(startedTodoIds.Contains).ToArray();
                string[] completed = request.ExecutableTodoIds.Where(completedTodoIds.Contains).ToArray();
                string[] unresolved = request.ExecutableTodoIds.Where(id => !completedTodoIds.Contains(id)).ToArray();
                string terminalStatus = synthesizeCancellation ? "cancelled" : "failed";
                string transportOutcome = synthesizeCancellation ? "cancelled" : "crashed";
                int terminalExitCode = synthesizeCancellation ? 0 : 70;
                var terminal = new Dictionary<string, object>
                {
                    { "schemaVersion", 1 }, { "protocolVersion", ProtocolVersion },
                    { "eventType", "run_terminal" }, { "sequence", nextSequence },
                    { "runId", request.RunId }, { "runAttemptId", request.RunAttemptId },
                    { "fencingToken", request.FencingToken }, { "gameId", request.GameId },
                    { "at", DateTimeOffset.UtcNow.ToString("o") }, { "status", terminalStatus },
                    { "transportOutcome", transportOutcome }, { "attemptedTodoInstanceIds", attempted },
                    { "completedTodoInstanceIds", completed }, { "unresolvedTodoInstanceIds", unresolved },
                    { "terminalEventDigest", ComputeTerminalDigest(request, terminalStatus, attempted, completed, unresolved, terminalExitCode) },
                    { "exitCode", terminalExitCode }
                };
                ProtocolOutput.WriteLine(Json.Serialize(terminal));
                Console.Error.WriteLine("adapter_host_terminalized:" + forcedTerminalReason);
                terminalSeen = true;
                declaredExit = terminalExitCode;
            }
            int actualExit = synthesizeTerminal ? declaredExit : (runner.HasExited ? runner.ExitCode : 70);
            try { runner.StandardInput.Close(); } catch { }
            // The runner and every automation-tool process it created belong to
            // this per-run job. Closing the handle is the final cleanup fence:
            // a leaked helper cannot survive into the next selected game.
            if (runnerJob != IntPtr.Zero) CloseHandle(runnerJob);
            runner.Dispose();
            if (!valid || !helloSeen || !terminalSeen || declaredExit != actualExit ||
                Interlocked.CompareExchange(ref stderrOverflow, 0, 0) != 0 ||
                Interlocked.CompareExchange(ref controlState.Failed, 0, 0) != 0)
                return 70;
            return actualExit;
        }

        private static IntPtr CreateKillOnCloseJob()
        {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero) return IntPtr.Zero;
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION information =
                new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            information.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
            int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr buffer = Marshal.AllocHGlobal(length);
            try
            {
                Marshal.StructureToPtr(information, buffer, false);
                if (!SetInformationJobObject(job, 9, buffer, (uint)length))
                {
                    CloseHandle(job);
                    return IntPtr.Zero;
                }
                return job;
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public IntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr securityAttributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr job,
            int informationClass,
            IntPtr information,
            uint informationLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        private static void RelayControls(ExecuteRequest request, Process runner, ControlState state)
        {
            while (!runner.HasExited)
            {
                string line;
                bool eof;
                if (!ReadLineBounded(ProtocolInput, MaxEventBytes, out line, out eof))
                {
                    if (!eof) Interlocked.Exchange(ref state.Failed, 1);
                    return;
                }
                if (!ValidateCancel(line, request))
                {
                    Interlocked.Exchange(ref state.Failed, 1);
                    return;
                }
                if (!WriteRunnerInput(runner, line)) return;
                MarkCancellationRequested(state);
            }
        }

        private static void RelayDurableCancel(ExecuteRequest request, Process runner, ControlState state)
        {
            string pendingPath;
            string deliveredPath;
            try
            {
                GetDurableCancelPaths(request, out pendingPath, out deliveredPath);
            }
            catch (Exception failure)
            {
                Console.Error.WriteLine("adapter_host_durable_control_error:" + failure.Message);
                Interlocked.Exchange(ref state.Failed, 1);
                return;
            }
            while (!runner.HasExited && Interlocked.CompareExchange(ref state.CancelRequested, 0, 0) == 0)
            {
                if (!File.Exists(pendingPath))
                {
                    Thread.Sleep(250);
                    continue;
                }
                string text;
                IDictionary<string, object> control;
                try
                {
                    FileInfo item = new FileInfo(pendingPath);
                    if ((item.Attributes & FileAttributes.ReparsePoint) != 0 || item.Length < 2 || item.Length > MaxEventBytes)
                        throw new InvalidDataException("durable_cancel_file_invalid");
                    text = File.ReadAllText(pendingPath, new UTF8Encoding(false, true));
                    if (!ValidateDurableCancel(text, request, out control))
                        throw new InvalidDataException("durable_cancel_authentication_failed");
                    var runnerControl = new Dictionary<string, object>
                    {
                        { "schemaVersion", 1 }, { "protocolVersion", ProtocolVersion }, { "controlType", "cancel" },
                        { "runId", request.RunId }, { "runAttemptId", request.RunAttemptId },
                        { "fencingToken", request.FencingToken }, { "at", GetString(control, "at") },
                        { "reasonCode", GetString(control, "reasonCode") }
                    };
                    if (!WriteRunnerInput(runner, Json.Serialize(runnerControl))) return;
                    if (File.Exists(deliveredPath))
                        throw new IOException("durable_cancel_delivery_ack_exists");
                    File.Move(pendingPath, deliveredPath);
                    MarkCancellationRequested(state);
                    return;
                }
                catch (Exception failure)
                {
                    Console.Error.WriteLine("adapter_host_durable_control_error:" + failure.Message);
                    Interlocked.Exchange(ref state.Failed, 1);
                    return;
                }
            }
        }

        private static void MarkCancellationRequested(ControlState state)
        {
            if (Interlocked.CompareExchange(ref state.CancelRequested, 1, 0) == 0)
                Interlocked.Exchange(ref state.CancelDeadlineUtcTicks,
                    DateTimeOffset.UtcNow.AddSeconds(CancelGraceSeconds).UtcDateTime.Ticks);
        }

        private static bool WriteRunnerInput(Process runner, string line)
        {
            try
            {
                lock (RunnerInputGate)
                {
                    if (runner.HasExited) return false;
                    runner.StandardInput.WriteLine(line);
                    runner.StandardInput.Flush();
                    return true;
                }
            }
            catch { return false; }
        }

        private static void GetDurableCancelPaths(ExecuteRequest request, out string pendingPath, out string deliveredPath)
        {
            string commonValue = Environment.GetEnvironmentVariable("ProgramData");
            if (String.IsNullOrWhiteSpace(commonValue))
                commonValue = Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData);
            if (String.IsNullOrWhiteSpace(commonValue)) throw new InvalidDataException("program_data_unavailable");
            string common = Path.GetFullPath(commonValue).TrimEnd(Path.DirectorySeparatorChar);
            string runtime = Path.GetFullPath(Path.Combine(common, "YeYuGamer", "runtime"));
            string root = Path.GetFullPath(Path.Combine(runtime, "adapter-controls"));
            if (!root.StartsWith(runtime + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("durable_cancel_root_escaped_runtime");
            EnsureNoReparseDirectoryPath(common, root);
            EnsureProtectedControlDirectory(root);
            EnsureNoReparseDirectoryPath(common, root);
            string leaf = request.RunAttemptId + ".cancel.json";
            pendingPath = Path.GetFullPath(Path.Combine(root, leaf));
            deliveredPath = Path.GetFullPath(Path.Combine(root, request.RunAttemptId + ".delivered.json"));
            if (!pendingPath.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) ||
                !deliveredPath.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("durable_cancel_path_escaped_root");
        }

        private static void EnsureNoReparseDirectoryPath(string trustedRoot, string target)
        {
            string root = Path.GetFullPath(trustedRoot).TrimEnd(Path.DirectorySeparatorChar);
            string full = Path.GetFullPath(target).TrimEnd(Path.DirectorySeparatorChar);
            if (!full.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("durable_cancel_root_escaped_runtime");
            string cursor = root;
            string relative = full.Substring(root.Length + 1);
            foreach (string component in relative.Split(new char[] { Path.DirectorySeparatorChar }, StringSplitOptions.RemoveEmptyEntries))
            {
                cursor = Path.Combine(cursor, component);
                bool directoryExists = Directory.Exists(cursor);
                bool fileExists = File.Exists(cursor);
                if (!directoryExists && !fileExists) continue;
                if (!directoryExists || (File.GetAttributes(cursor) & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidDataException("durable_cancel_root_reparse_point");
            }
        }

        private static void EnsureProtectedControlDirectory(string root)
        {
            WindowsIdentity identity = WindowsIdentity.GetCurrent();
            if (identity == null || identity.User == null)
                throw new InvalidDataException("durable_cancel_identity_unavailable");
            SecurityIdentifier currentUser = identity.User;
            SecurityIdentifier localSystem = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
            SecurityIdentifier administrators = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);
            SecurityIdentifier[] allowed = new SecurityIdentifier[] { currentUser, localSystem, administrators };
            DirectorySecurity desired = new DirectorySecurity();
            desired.SetAccessRuleProtection(true, false);
            foreach (SecurityIdentifier sid in allowed)
                desired.AddAccessRule(new FileSystemAccessRule(
                    sid, FileSystemRights.FullControl,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                    PropagationFlags.None, AccessControlType.Allow));
            Directory.CreateDirectory(root);
            Directory.SetAccessControl(root, desired);

            DirectorySecurity actual = Directory.GetAccessControl(root, AccessControlSections.Access);
            if (!actual.AreAccessRulesProtected)
                throw new InvalidDataException("durable_cancel_acl_inheritance_enabled");
            var expected = new HashSet<string>(allowed.Select(item => item.Value), StringComparer.OrdinalIgnoreCase);
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            AuthorizationRuleCollection rules = actual.GetAccessRules(true, false, typeof(SecurityIdentifier));
            foreach (AuthorizationRule authorizationRule in rules)
            {
                FileSystemAccessRule rule = authorizationRule as FileSystemAccessRule;
                SecurityIdentifier sid = rule == null ? null : rule.IdentityReference as SecurityIdentifier;
                if (rule == null || sid == null || rule.IsInherited || !expected.Contains(sid.Value) ||
                    rule.AccessControlType != AccessControlType.Allow ||
                    (rule.FileSystemRights & FileSystemRights.FullControl) != FileSystemRights.FullControl ||
                    (rule.InheritanceFlags & (InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit)) !=
                        (InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit))
                    throw new InvalidDataException("durable_cancel_acl_invalid");
                seen.Add(sid.Value);
            }
            if (!seen.SetEquals(expected))
                throw new InvalidDataException("durable_cancel_acl_missing_principal");
        }

        private static bool ValidateDurableCancel(string text, ExecuteRequest request, out IDictionary<string, object> value)
        {
            value = null;
            long schema;
            DateTimeOffset at;
            if (!TryObject(text, out value) || !HasExactKeys(value, new string[]
            {
                "schemaVersion", "protocolVersion", "controlType", "runId", "runAttemptId",
                "at", "reasonCode", "nonce", "authorityMac"
            }) || !TryInteger(value, "schemaVersion", out schema) || schema != 1 ||
                !IsString(value, "protocolVersion", ProtocolVersion) || !IsString(value, "controlType", "cancel") ||
                !IsString(value, "runId", request.RunId) || !IsString(value, "runAttemptId", request.RunAttemptId) ||
                !TryTime(value, "at", out at) || at < request.IssuedAt.AddMinutes(-1) ||
                at > request.ExecutionDeadline || at > DateTimeOffset.UtcNow.AddMinutes(5) ||
                !IsIdentifier(GetString(value, "reasonCode")) ||
                !Token.IsMatch(GetString(value, "nonce") ?? String.Empty) ||
                !AuthorityMac.IsMatch(GetString(value, "authorityMac") ?? String.Empty)) return false;
            string canonical = ProtocolVersion + "\n" + "cancel" + "\n" + request.RunId + "\n" +
                request.RunAttemptId + "\n" + GetString(value, "at") + "\n" +
                GetString(value, "reasonCode") + "\n" + GetString(value, "nonce");
            byte[] key = Encoding.UTF8.GetBytes(request.CancelAuthority);
            byte[] expected;
            using (HMACSHA256 hmac = new HMACSHA256(key))
                expected = hmac.ComputeHash(Encoding.UTF8.GetBytes(canonical));
            string actualHex = GetString(value, "authorityMac").Substring("hmac-sha256:".Length);
            byte[] actual = new byte[32];
            for (int index = 0; index < actual.Length; index++)
                actual[index] = Byte.Parse(actualHex.Substring(index * 2, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
            int difference = 0;
            for (int index = 0; index < expected.Length; index++) difference |= expected[index] ^ actual[index];
            return difference == 0;
        }

        private static bool ValidateCancel(string text, ExecuteRequest request)
        {
            IDictionary<string, object> value;
            long schema;
            DateTimeOffset at;
            return TryObject(text, out value) && HasExactKeys(value, new string[]
            {
                "schemaVersion", "protocolVersion", "controlType", "runId", "runAttemptId",
                "fencingToken", "at", "reasonCode"
            }) && TryInteger(value, "schemaVersion", out schema) && schema == 1 &&
                IsString(value, "protocolVersion", ProtocolVersion) && IsString(value, "controlType", "cancel") &&
                IsString(value, "runId", request.RunId) && IsString(value, "runAttemptId", request.RunAttemptId) &&
                IsString(value, "fencingToken", request.FencingToken) && TryTime(value, "at", out at) &&
                IsIdentifier(GetString(value, "reasonCode"));
        }

        private static bool ValidateRunnerEvent(string text, ExecuteRequest request, ref int sequence, ref bool hello,
            ref bool terminal, ref int declaredExit, HashSet<string> startedTodoIds, HashSet<string> completedTodoIds)
        {
            if (terminal) return false;
            IDictionary<string, object> value;
            long schema;
            long current;
            DateTimeOffset at;
            if (!TryObject(text, out value) || !AllowedEventTypes.Contains(GetString(value, "eventType") ?? String.Empty) ||
                !TryInteger(value, "schemaVersion", out schema) || schema != 1 ||
                !IsString(value, "protocolVersion", ProtocolVersion) ||
                !TryInteger(value, "sequence", out current) || current != sequence ||
                !IsString(value, "runId", request.RunId) || !IsString(value, "runAttemptId", request.RunAttemptId) ||
                !IsString(value, "fencingToken", request.FencingToken) || !IsString(value, "gameId", request.GameId) ||
                !TryTime(value, "at", out at)) return false;
            string eventType = GetString(value, "eventType");
            if (!hello && eventType != "hello") return false;
            if (eventType == "hello")
            {
                List<object> accepted;
                if (hello || !IsString(value, "packageId", ExecutionPackageId) ||
                    !TryArray(value, "acceptedTodoInstanceIds", out accepted) || !ArrayEquals(accepted, request.ExecutableTodoIds))
                    return false;
                hello = true;
            }
            else if (eventType == "run_terminal")
            {
                long exit;
                List<object> attempted;
                List<object> completed;
                List<object> unresolved;
                string status = GetString(value, "status") ?? String.Empty;
                if (!HasExactKeys(value, new string[]
                    {
                        "schemaVersion", "protocolVersion", "eventType", "sequence", "runId", "runAttemptId",
                        "fencingToken", "gameId", "at", "status", "transportOutcome",
                        "attemptedTodoInstanceIds", "completedTodoInstanceIds", "unresolvedTodoInstanceIds",
                        "terminalEventDigest", "exitCode"
                    }) ||
                    !AllowedRunTerminalStatuses.Contains(status) ||
                    !AllowedTransportOutcomes.Contains(GetString(value, "transportOutcome") ?? String.Empty) ||
                    !TryInteger(value, "exitCode", out exit) || exit < Int32.MinValue || exit > Int32.MaxValue ||
                    !TryArray(value, "attemptedTodoInstanceIds", out attempted) ||
                    !TryArray(value, "completedTodoInstanceIds", out completed) ||
                    !TryArray(value, "unresolvedTodoInstanceIds", out unresolved)) return false;
                string[] expectedAttempted = request.ExecutableTodoIds.Where(startedTodoIds.Contains).ToArray();
                string[] expectedCompleted = request.ExecutableTodoIds.Where(completedTodoIds.Contains).ToArray();
                string[] expectedUnresolved = request.ExecutableTodoIds.Where(id => !completedTodoIds.Contains(id)).ToArray();
                if (!ArrayEquals(attempted, expectedAttempted.ToList()) ||
                    !ArrayEquals(completed, expectedCompleted.ToList()) ||
                    !ArrayEquals(unresolved, expectedUnresolved.ToList()) ||
                    (status == "completed" && expectedUnresolved.Length != 0) ||
                    (status != "completed" && expectedUnresolved.Length == 0) ||
                    !String.Equals(GetString(value, "terminalEventDigest"),
                        ComputeTerminalDigest(request, status, expectedAttempted, expectedCompleted,
                            expectedUnresolved, (int)exit), StringComparison.Ordinal)) return false;
                declaredExit = (int)exit;
                terminal = true;
            }
            else
            {
                string todoInstanceId = GetString(value, "todoInstanceId") ?? String.Empty;
                if (!request.ExecutableTodoIds.Contains(todoInstanceId)) return false;
                if (eventType == "todo_attempt_started") startedTodoIds.Add(todoInstanceId);
                if (eventType == "todo_terminal")
                {
                    string status = GetString(value, "status") ?? String.Empty;
                    bool retryable;
                    if (!AllowedTodoTerminalStatuses.Contains(status) ||
                        !TryBoolean(value, "retryable", out retryable) ||
                        (status == "human_required" && retryable)) return false;
                    if (status == "completed") completedTodoIds.Add(todoInstanceId);
                }
            }
            sequence += 1;
            return true;
        }

        private static bool TryParseRequest(string text, ParsedArguments parsed, out ExecuteRequest request, out string error)
        {
            request = new ExecuteRequest();
            error = String.Empty;
            IDictionary<string, object> value;
            if (!TryObject(text, out value) || !HasExactKeys(value, new string[]
            {
                "schemaVersion", "protocolVersion", "requestType", "runId", "runAttemptId", "fencingToken",
                "cancelAuthority",
                "gameId", "cadence", "managerStateVersion", "catalogVersion", "policyDigest", "issuedAt", "expiresAt",
                "timeoutSeconds", "preserveClientOnStop", "executableTodoInstanceIds", "todos"
            }, new [] { "accountId", "accountSnapshot" })) { error = "Request fields differ from protocol v1.1."; return false; }
            if (!ValidAccountScope(value)) { error = "Request account scope is invalid."; return false; }
            long schema;
            long stateVersion;
            long timeout;
            bool preserve;
            DateTimeOffset issued;
            DateTimeOffset expires;
            request.RunId = GetString(value, "runId");
            request.RunAttemptId = GetString(value, "runAttemptId");
            request.FencingToken = GetString(value, "fencingToken");
            request.CancelAuthority = GetString(value, "cancelAuthority");
            request.GameId = GetString(value, "gameId");
            string cadence = GetString(value, "cadence");
            if (!TryInteger(value, "schemaVersion", out schema) || schema != 1 ||
                !IsString(value, "protocolVersion", ProtocolVersion) || !IsString(value, "requestType", "execute") ||
                !IsUuid(request.RunId) || request.RunId != parsed.RunId || !IsUuid(request.RunAttemptId) ||
                !Token.IsMatch(request.FencingToken ?? String.Empty) || !Token.IsMatch(request.CancelAuthority ?? String.Empty) ||
                String.Equals(request.CancelAuthority, request.FencingToken, StringComparison.Ordinal) ||
                request.GameId != parsed.GameId || !AllowedGameIds.Contains(request.GameId) ||
                (cadence != "daily" && cadence != "weekly" && cadence != "manual") ||
                !TryInteger(value, "managerStateVersion", out stateVersion) || stateVersion < 0 ||
                !IsIdentifier(GetString(value, "catalogVersion")) || !Digest.IsMatch(GetString(value, "policyDigest") ?? String.Empty) ||
                !TryTime(value, "issuedAt", out issued) || !TryTime(value, "expiresAt", out expires) || expires <= issued || expires <= DateTimeOffset.UtcNow ||
                !TryInteger(value, "timeoutSeconds", out timeout) || timeout < 1 || timeout > 86400 || timeout > (long)(expires - issued).TotalSeconds ||
                !TryBoolean(value, "preserveClientOnStop", out preserve) || !preserve)
            { error = "Request scope or safety policy is invalid."; return false; }
            DateTimeOffset executionDeadline = issued.AddSeconds(timeout);
            if (executionDeadline > expires) executionDeadline = expires;
            if (executionDeadline <= DateTimeOffset.UtcNow)
            { error = "Request execution deadline has already elapsed."; return false; }
            request.TimeoutSeconds = timeout;
            request.IssuedAt = issued;
            request.ExecutionDeadline = executionDeadline;
            List<object> ids;
            List<object> todos;
            if (!TryArray(value, "executableTodoInstanceIds", out ids) || !TryArray(value, "todos", out todos) ||
                ids.Count < 1 || ids.Count > 64 || todos.Count != ids.Count)
            { error = "No executable Todo scope was provided."; return false; }
            HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
            for (int index = 0; index < ids.Count; index++)
            {
                string todoId = ids[index] as string;
                IDictionary<string, object> todo = todos[index] as IDictionary<string, object>;
                long definitionVersion;
                long priorAttempts;
                if (!IsTodoId(todoId) || !seen.Add(todoId) || todo == null || !HasExactKeys(todo, new string[]
                {
                    "todoInstanceId", "todoDefinitionId", "definitionVersion", "operation", "risk",
                    "adapterCapabilityRef", "priorAttempts", "executionDisposition"
                }) || !IsString(todo, "todoInstanceId", todoId) || !IsIdentifier(GetString(todo, "todoDefinitionId")) ||
                    !TryInteger(todo, "definitionVersion", out definitionVersion) || definitionVersion < 1 ||
                    !IsIdentifier(GetString(todo, "operation")) ||
                    (GetString(todo, "risk") != "observe_only" && GetString(todo, "risk") != "routine_action") ||
                    !IsCapability(GetString(todo, "adapterCapabilityRef")) ||
                    !TryInteger(todo, "priorAttempts", out priorAttempts) || priorAttempts < 0 ||
                    !IsString(todo, "executionDisposition", "executable"))
                { error = "Todo target is not executable."; return false; }
                request.ExecutableTodoIds.Add(todoId);
                request.Todos.Add(new TodoScope
                {
                    TodoDefinitionId = GetString(todo, "todoDefinitionId"),
                    Operation = GetString(todo, "operation"),
                    Risk = GetString(todo, "risk"),
                    CapabilityRef = GetString(todo, "adapterCapabilityRef")
                });
            }
            value.Remove("cancelAuthority");
            request.RunnerRequestJson = Json.Serialize(value);
            return true;
        }

        private static bool TryLoadPackage(ExecuteRequest request, out ExecutionPackage package, out string error)
        {
            package = new ExecutionPackage();
            error = String.Empty;
            string hostRoot = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            string adaptersRoot = Path.GetDirectoryName(hostRoot);
            string root;
            if (!TryResolveExecutionPackageRoot(adaptersRoot, request.GameId, out root))
            { error = "Execution package root is outside the Manager module registry."; return false; }
            string manifestPath = Path.Combine(root, "install-manifest.json");
            if (!Directory.Exists(root) || IsReparse(root) || !File.Exists(manifestPath) || IsReparse(manifestPath) ||
                new FileInfo(manifestPath).Length < 1 || new FileInfo(manifestPath).Length > MaxManifestBytes)
            { error = "Promoted execution package is missing or unsafe."; return false; }
            IDictionary<string, object> manifest;
            if (!TryObject(File.ReadAllText(manifestPath, Encoding.UTF8), out manifest) || !HasExactKeys(manifest, new string[]
            {
                "schemaVersion", "packageId", "packageVersion", "buildId", "builtAt", "installedAt", "protocolVersions",
                "hostPackageId", "minHostVersion", "entryPoint", "files", "supportedGameIds", "operationBindings",
                "forbiddenOperationClasses", "limits", "artifactPolicy", "security", "promotion", "executionReady"
            })) { error = "Execution manifest fields are invalid."; return false; }
            long schema;
            bool ready;
            DateTimeOffset built;
            DateTimeOffset installed;
            List<object> protocols;
            List<object> games;
            if (!TryInteger(manifest, "schemaVersion", out schema) || schema != 2 ||
                !IsString(manifest, "packageId", ExecutionPackageId) || !IsIdentifier(GetString(manifest, "packageVersion")) ||
                !IsIdentifier(GetString(manifest, "buildId")) || !TryTime(manifest, "builtAt", out built) ||
                !TryTime(manifest, "installedAt", out installed) || !IsString(manifest, "hostPackageId", "manager-adapter-host") ||
                !IsIdentifier(GetString(manifest, "minHostVersion")) || !TryBoolean(manifest, "executionReady", out ready) || !ready ||
                !TryArray(manifest, "protocolVersions", out protocols) || !Contains(protocols, ProtocolVersion) ||
                !TryArray(manifest, "supportedGameIds", out games) || !Contains(games, request.GameId))
            { error = "Execution manifest identity or scope is invalid."; return false; }

            List<object> forbiddenValues;
            if (!TryArray(manifest, "forbiddenOperationClasses", out forbiddenValues))
            { error = "Forbidden operation classes are missing."; return false; }
            HashSet<string> forbidden = new HashSet<string>(StringComparer.Ordinal);
            foreach (object item in forbiddenValues)
            {
                string name = item as string;
                if (!IsIdentifier(name) || !forbidden.Add(name)) { error = "Forbidden operation classes are invalid."; return false; }
            }
            if (!forbidden.IsSupersetOf(RequiredForbiddenClasses))
            { error = "Hard-denied operation classes are incomplete."; return false; }
            IDictionary<string, object> security = GetObject(manifest, "security");
            bool command;
            bool path;
            bool input;
            if (security == null || !HasExactKeys(security, new string[] { "allowsArbitraryCommand", "allowsArbitraryPath", "allowsArbitraryInput" }) ||
                !TryBoolean(security, "allowsArbitraryCommand", out command) || command ||
                !TryBoolean(security, "allowsArbitraryPath", out path) || path ||
                !TryBoolean(security, "allowsArbitraryInput", out input) || input)
            { error = "Execution package exposes an arbitrary input surface."; return false; }
            IDictionary<string, object> promotion = GetObject(manifest, "promotion");
            if (promotion == null || !HasExactKeys(promotion, new string[]
                {
                    "status", "replaySuiteDigest", "shadowSuiteDigest", "canarySuiteDigest",
                    "payloadDigest", "receiptFile", "receiptSha256", "receiptResourceId"
                }) ||
                !IsString(promotion, "status", "promoted") || !IsNonZeroDigest(GetString(promotion, "replaySuiteDigest")) ||
                !IsNonZeroDigest(GetString(promotion, "shadowSuiteDigest")) ||
                !IsNonZeroDigest(GetString(promotion, "canarySuiteDigest")) ||
                !IsNonZeroDigest(GetString(promotion, "payloadDigest")) ||
                !IsString(promotion, "receiptFile", "promotion-receipt.json") ||
                !IsNonZeroDigest(GetString(promotion, "receiptSha256")) ||
                !IsUuid(GetString(promotion, "receiptResourceId")))
            { error = "Execution package has not passed promotion."; return false; }

            List<object> files;
            if (!TryArray(manifest, "files", out files) || files.Count < 1 || files.Count > 128)
            { error = "Execution package file manifest is invalid."; return false; }
            HashSet<string> declared = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var declaredHashes = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var payloadEntries = new List<string>();
            foreach (object fileObject in files)
            {
                IDictionary<string, object> file = fileObject as IDictionary<string, object>;
                long size;
                string relative = file == null ? null : GetString(file, "path");
                string hash = file == null ? null : GetString(file, "sha256");
                string full;
                if (file == null || !HasExactKeys(file, new string[] { "path", "sha256", "sizeBytes" }) ||
                    !SafePath(root, relative, out full) || relative == "install-manifest.json" || !declared.Add(relative) ||
                    !Regex.IsMatch(hash ?? String.Empty, "^[0-9a-f]{64}$") || !TryInteger(file, "sizeBytes", out size) || size < 1 ||
                    !File.Exists(full) || IsReparse(full) || new FileInfo(full).Length != size || ComputeSha256(full) != hash)
                { error = "Execution package file integrity failed."; return false; }
                declaredHashes[relative] = hash;
                if (!String.Equals(relative, "promotion-receipt.json", StringComparison.OrdinalIgnoreCase))
                    payloadEntries.Add(relative + "\0" + size.ToString(CultureInfo.InvariantCulture) + "\0" + hash + "\n");
            }
            payloadEntries.Sort(StringComparer.Ordinal);
            string computedPayloadDigest = ComputeSha256Text(String.Concat(payloadEntries));
            if (!String.Equals(computedPayloadDigest, GetString(promotion, "payloadDigest"), StringComparison.Ordinal) ||
                !declared.Contains("promotion-receipt.json") ||
                !String.Equals("sha256:" + declaredHashes["promotion-receipt.json"], GetString(promotion, "receiptSha256"), StringComparison.Ordinal))
            { error = "Promotion receipt is not bound to the installed payload."; return false; }
            string receiptPath;
            if (!SafePath(root, "promotion-receipt.json", out receiptPath) || !ValidatePromotionReceipt(
                receiptPath, manifest, promotion, games, computedPayloadDigest))
            { error = "Promotion receipt is invalid or belongs to another package."; return false; }
            string entryRelative = GetString(manifest, "entryPoint");
            string entryPoint;
            if (entryRelative != "runner.exe" || !declared.Contains(entryRelative) || !SafePath(root, entryRelative, out entryPoint))
            { error = "Execution entryPoint is invalid."; return false; }
            HashSet<string> actual = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (string item in Directory.GetFileSystemEntries(root, "*", SearchOption.AllDirectories))
            {
                if (IsReparse(item)) { error = "Execution package contains a reparse point."; return false; }
                if (File.Exists(item)) actual.Add(RelativePath(root, item));
            }
            declared.Add("install-manifest.json");
            if (!actual.SetEquals(declared)) { error = "Execution package contains missing or unexpected files."; return false; }

            IDictionary<string, object> allBindings = GetObject(manifest, "operationBindings");
            IDictionary<string, object> bindings = allBindings == null ? null : GetObject(allBindings, request.GameId);
            if (bindings == null || bindings.Count == 0) { error = "Game has no promoted operation binding."; return false; }
            long maximumPlanSeconds = 0;
            foreach (TodoScope todo in request.Todos)
            {
                IDictionary<string, object> binding = GetObject(bindings, todo.Operation);
                long timeout;
                bool resume;
                List<object> definitions;
                List<object> capabilities;
                List<object> evidence;
                string mode = binding == null ? null : GetString(binding, "mode");
                string actionClass = binding == null ? null : GetString(binding, "actionClass");
                if (binding == null || !HasExactKeys(binding, new string[]
                {
                    "handlerId", "mode", "actionClass", "risk", "todoDefinitionIds", "adapterCapabilityRefs",
                    "supportsResume", "timeoutSeconds", "requiredEvidenceKinds"
                }) || !IsIdentifier(GetString(binding, "handlerId")) || (mode != "granular" && mode != "whole_run") ||
                    !IsIdentifier(actionClass) || forbidden.Contains(actionClass) || !IsString(binding, "risk", todo.Risk) ||
                    !TryArray(binding, "todoDefinitionIds", out definitions) || !Contains(definitions, todo.TodoDefinitionId) ||
                    !TryArray(binding, "adapterCapabilityRefs", out capabilities) || !Contains(capabilities, todo.CapabilityRef) ||
                    !TryBoolean(binding, "supportsResume", out resume) || !TryInteger(binding, "timeoutSeconds", out timeout) || timeout < 1 ||
                    !TryArray(binding, "requiredEvidenceKinds", out evidence) || evidence.Count == 0)
                { error = "Todo binding differs from its promoted contract."; return false; }
                maximumPlanSeconds += timeout;
            }
            if (request.TimeoutSeconds > maximumPlanSeconds)
            { error = "Plan timeout exceeds promoted handler limits."; return false; }
            package.Root = root;
            package.EntryPoint = entryPoint;
            return true;
        }

        private static bool TryResolveExecutionPackageRoot(string adaptersRoot, string gameId, out string root)
        {
            root = null;
            if (String.IsNullOrEmpty(adaptersRoot) || !AllowedGameIds.Contains(gameId)) return false;
            string legacyRoot = Path.GetFullPath(Path.Combine(adaptersRoot, ExecutionPackageId));
            string modulesRoot = Path.GetFullPath(Path.Combine(adaptersRoot, "game-modules"));
            string moduleRoot = Path.GetFullPath(Path.Combine(modulesRoot, gameId.ToLowerInvariant()));
            string configured = Environment.GetEnvironmentVariable("YEYU_GAMER_EXECUTION_PACKAGE_ROOT");
            string selected = String.IsNullOrWhiteSpace(configured) ? legacyRoot : Path.GetFullPath(configured);
            if (!String.Equals(selected, legacyRoot, StringComparison.OrdinalIgnoreCase) &&
                !String.Equals(selected, moduleRoot, StringComparison.OrdinalIgnoreCase)) return false;
            root = selected;
            return true;
        }

        private static bool TryParseArguments(string[] args, out ParsedArguments parsed, out string error)
        {
            parsed = new ParsedArguments();
            error = String.Empty;
            if (args == null) { error = "arguments are required"; return false; }
            for (int index = 0; index < args.Length; index += 2)
            {
                if (index + 1 >= args.Length) { error = "every option requires one value"; return false; }
                string key = args[index];
                string value = args[index + 1];
                if (key == "--operation") parsed.Operation = value;
                else if (key == "--protocol-version") parsed.Protocol = value;
                else if (key == "--run-id") parsed.RunId = value;
                else if (key == "--game-id") parsed.GameId = value;
                else { error = "unknown option"; return false; }
            }
            if (String.IsNullOrEmpty(parsed.Operation)) parsed.Operation = "execute";
            if (parsed.Operation != "probe" && parsed.Operation != "canary" && parsed.Operation != "execute")
            { error = "unsupported operation"; return false; }
            if (parsed.Protocol != ProtocolVersion) { error = "protocol version mismatch"; return false; }
            if (parsed.Operation == "probe")
            {
                if (!String.IsNullOrEmpty(parsed.RunId) || !String.IsNullOrEmpty(parsed.GameId))
                { error = "probe does not accept run or game scope"; return false; }
                return true;
            }
            if (!IsUuid(parsed.RunId)) { error = "run-id must be a lowercase canonical UUID"; return false; }
            if (!AllowedGameIds.Contains(parsed.GameId ?? String.Empty)) { error = "game-id is not in the compiled allowlist"; return false; }
            return true;
        }

        private static void WriteResult(bool success, string code, ParsedArguments parsed, string message)
        {
            Dictionary<string, object> value = new Dictionary<string, object>();
            value["protocolVersion"] = ProtocolVersion;
            value["hostVersion"] = HostVersion;
            value["operation"] = parsed.Operation ?? String.Empty;
            value["success"] = success;
            value["code"] = code;
            value["message"] = message;
            value["runId"] = parsed.RunId ?? String.Empty;
            value["gameId"] = parsed.GameId ?? String.Empty;
            value["hostReady"] = true;
            value["executionReady"] = false;
            value["adapterProcessStarted"] = false;
            value["gameProcessStarted"] = false;
            value["entryPointSha256"] = ComputeSha256(Assembly.GetExecutingAssembly().Location);
            List<string> games = new List<string>(AllowedGameIds);
            games.Sort(StringComparer.Ordinal);
            value["supportedGameIds"] = games;
            ProtocolOutput.WriteLine(Json.Serialize(value));
        }

        private static bool TryObject(string text, out IDictionary<string, object> value)
        {
            value = null;
            if (String.IsNullOrWhiteSpace(text) || Encoding.UTF8.GetByteCount(text) > MaxRequestBytes) return false;
            try { value = Json.DeserializeObject(text) as IDictionary<string, object>; return value != null; }
            catch { return false; }
        }

        private static bool ReadLineBounded(TextReader reader, int maximumBytes, out string value, out bool eof)
        {
            StringBuilder builder = new StringBuilder();
            eof = false;
            while (true)
            {
                int read = reader.Read();
                if (read < 0)
                {
                    eof = builder.Length == 0;
                    value = builder.ToString();
                    return builder.Length > 0;
                }
                char character = (char)read;
                if (character == '\n')
                {
                    value = builder.ToString();
                    return value.Length > 0 && Encoding.UTF8.GetByteCount(value) <= maximumBytes;
                }
                if (character == '\r') continue;
                builder.Append(character);
                if (builder.Length > maximumBytes) { value = String.Empty; return false; }
            }
        }

        private static bool HasExactKeys(IDictionary<string, object> value, string[] names, string[] optional = null)
        {
            if (value == null || value.Count < names.Length || value.Count > names.Length + (optional == null ? 0 : optional.Length)) return false;
            foreach (string name in names) if (!value.ContainsKey(name)) return false;
            foreach (string key in value.Keys) if (!names.Contains(key) && (optional == null || !optional.Contains(key))) return false;
            return true;
        }
        private static bool ValidAccountScope(IDictionary<string, object> value)
        {
            if (!value.ContainsKey("accountId") && !value.ContainsKey("accountSnapshot")) return true;
            if (GetString(value, "gameId") != "WW") return false;
            string accountId = value.ContainsKey("accountId") ? GetString(value, "accountId") : "default";
            if (accountId != "default" && !IsUuid(accountId)) return false;
            if (!value.ContainsKey("accountSnapshot")) return accountId == "default";
            IDictionary<string, object> snapshot = GetObject(value, "accountSnapshot");
            if (snapshot == null) return false;
            if (snapshot.Count == 0) return accountId == "default";
            if (!HasExactKeys(snapshot, new [] { "label", "saved_account_label" })) return false;
            foreach (string key in new [] { "label", "saved_account_label" }) {
                string text = GetString(snapshot, key);
                if (String.IsNullOrWhiteSpace(text) || text != text.Trim() || text.Length > (key == "label" ? 80 : 160) ||
                    text.Any(character => Char.IsControl(character) || character == '\\' || character == '/')) return false;
            }
            return GetString(snapshot, "saved_account_label").Contains("****");
        }
        private static string GetString(IDictionary<string, object> value, string name)
        {
            object item;
            return value != null && value.TryGetValue(name, out item) ? item as string : null;
        }
        private static bool IsString(IDictionary<string, object> value, string name, string expected)
        { return String.Equals(GetString(value, name), expected, StringComparison.Ordinal); }
        private static IDictionary<string, object> GetObject(IDictionary<string, object> value, string name)
        {
            object item;
            return value != null && value.TryGetValue(name, out item) ? item as IDictionary<string, object> : null;
        }
        private static bool TryArray(IDictionary<string, object> value, string name, out List<object> result)
        {
            result = new List<object>();
            object item;
            if (value == null || !value.TryGetValue(name, out item) || item is string) return false;
            IEnumerable sequence = item as IEnumerable;
            if (sequence == null) return false;
            foreach (object entry in sequence) result.Add(entry);
            return true;
        }
        private static bool TryInteger(IDictionary<string, object> value, string name, out long result)
        {
            result = 0;
            object item;
            if (value == null || !value.TryGetValue(name, out item) || item is bool) return false;
            try
            {
                result = Convert.ToInt64(item, CultureInfo.InvariantCulture);
                return Convert.ToDecimal(item, CultureInfo.InvariantCulture) == result;
            }
            catch { return false; }
        }
        private static bool TryBoolean(IDictionary<string, object> value, string name, out bool result)
        {
            result = false;
            object item;
            if (value == null || !value.TryGetValue(name, out item) || !(item is bool)) return false;
            result = (bool)item;
            return true;
        }
        private static bool TryTime(IDictionary<string, object> value, string name, out DateTimeOffset result)
        {
            result = DateTimeOffset.MinValue;
            string text = GetString(value, name);
            return text != null && IsoTimestampWithOffset.IsMatch(text) &&
                DateTimeOffset.TryParse(text, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out result);
        }
        private static bool IsUuid(string value)
        {
            Guid parsed;
            return value != null && Guid.TryParseExact(value, "D", out parsed) && parsed.ToString("D") == value;
        }
        private static bool IsTodoId(string value)
        {
            Match match = TodoId.Match(value ?? String.Empty);
            return match.Success && IsUuid(match.Groups[1].Value);
        }
        private static bool IsIdentifier(string value) { return value != null && Identifier.IsMatch(value); }
        private static bool IsCapability(string value) { return value != null && Capability.IsMatch(value); }
        private static bool Contains(List<object> values, string expected)
        {
            foreach (object value in values) if (String.Equals(value as string, expected, StringComparison.Ordinal)) return true;
            return false;
        }
        private static bool ArrayEquals(List<object> values, List<string> expected)
        {
            if (values.Count != expected.Count) return false;
            for (int index = 0; index < values.Count; index++) if ((values[index] as string) != expected[index]) return false;
            return true;
        }
        private static bool SafePath(string root, string relative, out string full)
        {
            full = String.Empty;
            if (String.IsNullOrEmpty(relative) || relative.Contains("\\") || Path.IsPathRooted(relative)) return false;
            string candidate = root;
            foreach (string part in relative.Split('/'))
            {
                if (String.IsNullOrEmpty(part) || part == "." || part == "..") return false;
                candidate = Path.Combine(candidate, part);
            }
            candidate = Path.GetFullPath(candidate);
            string prefix = root.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!candidate.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) return false;
            full = candidate;
            return true;
        }
        private static string RelativePath(string root, string full)
        {
            string prefix = root.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            return full.Substring(prefix.Length).Replace(Path.DirectorySeparatorChar, '/');
        }
        private static bool IsReparse(string path)
        {
            try { return (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0; }
            catch { return true; }
        }
        private static string ComputeSha256(string path)
        {
            using (FileStream stream = File.OpenRead(path))
            using (SHA256 sha = SHA256.Create())
            {
                byte[] bytes = sha.ComputeHash(stream);
                StringBuilder result = new StringBuilder(64);
                foreach (byte item in bytes) result.Append(item.ToString("x2"));
                return result.ToString();
            }
        }

        private static string ComputeSha256Text(string value)
        {
            using (SHA256 sha = SHA256.Create())
            {
                byte[] bytes = sha.ComputeHash(new UTF8Encoding(false).GetBytes(value));
                return "sha256:" + String.Concat(bytes.Select(item => item.ToString("x2")).ToArray());
            }
        }

        private static bool ValidatePromotionReceipt(string receiptPath, IDictionary<string, object> manifest,
            IDictionary<string, object> promotion, List<object> manifestGames, string payloadDigest)
        {
            try
            {
                IDictionary<string, object> receipt;
                if (!TryObject(File.ReadAllText(receiptPath, Encoding.UTF8), out receipt) || !HasExactKeys(receipt, new string[]
                    {
                        "schemaVersion", "resourceType", "resourceId", "state", "packageId", "packageVersion", "buildId",
                        "supportedGameIds", "payloadDigest", "replaySuiteDigest", "shadowSuiteDigest", "canarySuiteDigest",
                        "candidateTestEvidenceSha256", "managerCanaryEvidenceSha256", "hostEntryPointSha256", "issuedAt"
                    })) return false;
                long schema;
                DateTimeOffset issuedAt;
                List<object> receiptGames;
                if (!TryInteger(receipt, "schemaVersion", out schema) || schema != 1 ||
                    !IsString(receipt, "resourceType", "adapter-promotion-receipt") ||
                    !IsString(receipt, "state", "passed") ||
                    !IsUuid(GetString(receipt, "resourceId")) ||
                    !IsString(receipt, "resourceId", GetString(promotion, "receiptResourceId")) ||
                    !IsString(receipt, "packageId", ExecutionPackageId) ||
                    !IsString(receipt, "packageVersion", GetString(manifest, "packageVersion")) ||
                    !IsString(receipt, "buildId", GetString(manifest, "buildId")) ||
                    !TryArray(receipt, "supportedGameIds", out receiptGames) || !ArrayEquals(receiptGames, manifestGames.Cast<string>().ToList()) ||
                    !IsString(receipt, "payloadDigest", payloadDigest) ||
                    !IsString(receipt, "replaySuiteDigest", GetString(promotion, "replaySuiteDigest")) ||
                    !IsString(receipt, "shadowSuiteDigest", GetString(promotion, "shadowSuiteDigest")) ||
                    !IsString(receipt, "canarySuiteDigest", GetString(promotion, "canarySuiteDigest")) ||
                    !IsNonZeroDigest(GetString(receipt, "candidateTestEvidenceSha256")) ||
                    !IsNonZeroDigest(GetString(receipt, "managerCanaryEvidenceSha256")) ||
                    // The receipt records the Host that performed promotion,
                    // not the only compatible Host build. Manager verifies the
                    // current Host manifest; package/protocol and payload/receipt
                    // integrity are independently checked above on every run.
                    !IsNonZeroDigest(GetString(receipt, "hostEntryPointSha256")) ||
                    !TryTime(receipt, "issuedAt", out issuedAt)) return false;
                return true;
            }
            catch { return false; }
        }

        private static bool IsNonZeroDigest(string value)
        {
            if (value == null || !value.StartsWith("sha256:", StringComparison.Ordinal) || !Digest.IsMatch(value)) return false;
            string hex = value.Substring(7);
            return hex.Any(character => character != '0');
        }

        private static string ComputeTerminalDigest(ExecuteRequest request, string status, string[] attempted,
            string[] completed, string[] unresolved, int exitCode)
        {
            string payload = request.RunId + "\n" + request.RunAttemptId + "\n" + status + "\n" +
                String.Join("\n", attempted) + "\n--completed--\n" + String.Join("\n", completed) +
                "\n--unresolved--\n" + String.Join("\n", unresolved) + "\n" + exitCode.ToString(CultureInfo.InvariantCulture);
            using (SHA256 sha = SHA256.Create())
            {
                byte[] digest = sha.ComputeHash(new UTF8Encoding(false).GetBytes(payload));
                return "sha256:" + String.Concat(digest.Select(item => item.ToString("x2")).ToArray());
            }
        }

        private sealed class ParsedArguments
        {
            public string Operation { get; set; }
            public string Protocol { get; set; }
            public string RunId { get; set; }
            public string GameId { get; set; }
        }
        private sealed class ExecuteRequest
        {
            public ExecuteRequest() { ExecutableTodoIds = new List<string>(); Todos = new List<TodoScope>(); }
            public string RunId { get; set; }
            public string RunAttemptId { get; set; }
            public string FencingToken { get; set; }
            public string CancelAuthority { get; set; }
            public string GameId { get; set; }
            public long TimeoutSeconds { get; set; }
            public DateTimeOffset IssuedAt { get; set; }
            public DateTimeOffset ExecutionDeadline { get; set; }
            public string RunnerRequestJson { get; set; }
            public List<string> ExecutableTodoIds { get; private set; }
            public List<TodoScope> Todos { get; private set; }
        }
        private sealed class ControlState
        {
            public int Failed;
            public int CancelRequested;
            public long CancelDeadlineUtcTicks;
        }
        private sealed class TodoScope
        {
            public string TodoDefinitionId { get; set; }
            public string Operation { get; set; }
            public string Risk { get; set; }
            public string CapabilityRef { get; set; }
        }
        private sealed class ExecutionPackage
        {
            public string Root { get; set; }
            public string EntryPoint { get; set; }
        }
    }
}
