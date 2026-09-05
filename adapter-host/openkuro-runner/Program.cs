using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;

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
        private const int SwMaximize = 3;
        private const int SwShow = 5;
        private const uint MouseLeftDown = 0x0002;
        private const uint MouseLeftUp = 0x0004;
        private static readonly Dictionary<IntPtr, int> Gf2FormalStartClicks = new Dictionary<IntPtr, int>();

        [StructLayout(LayoutKind.Sequential)]
        private struct NativeRect
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
        }

        [DllImport("user32.dll")]
        private static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);

        [DllImport("user32.dll")]
        private static extern bool GetClientRect(IntPtr hWnd, out NativeRect rect);

        [DllImport("user32.dll")]
        private static extern bool GetWindowRect(IntPtr hWnd, out NativeRect rect);

        [DllImport("user32.dll")]
        private static extern bool IsWindowVisible(IntPtr hWnd);

        [DllImport("user32.dll")]
        private static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint flags);

        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr hWnd);

        [DllImport("user32.dll")]
        private static extern bool SetCursorPos(int x, int y);

        [DllImport("user32.dll")]
        private static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extraInfo);

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
            public string UpdateState;
            public string InnerPython;
            public string InnerEntry;
            public string AppName;
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
                    { "todoInstanceId", target.Item1 }, { "todoAttemptId", attempts[target.Item1] }, { "attemptNo", AttemptNumber(target.Item2) }, { "operation", operation }
                }));
            };
            var wwCaptureEvidence = new Dictionary<string, List<Tuple<string, string, bool, string>>>(StringComparer.Ordinal);
            var wwCaptureErrors = new Dictionary<string, string>(StringComparer.Ordinal);
            int wwDailyActivityPoints = -1;
            Action<string, string, string> captureWwEvidence = (operation, phase, stageDetail) => {
                if (binding.GameId != "WW" || operation != "claim-daily-reward") return;
                if (phase == "before") {
                    const string marker = "daily_activity_points=";
                    int markerIndex = (stageDetail ?? String.Empty).IndexOf(marker, StringComparison.Ordinal);
                    int parsedPoints;
                    if (markerIndex >= 0) {
                        string value = (stageDetail ?? String.Empty).Substring(markerIndex + marker.Length);
                        int separator = value.IndexOf(';');
                        if (separator >= 0) value = value.Substring(0, separator);
                        if (Int32.TryParse(value.Trim(), out parsedPoints)) wwDailyActivityPoints = parsedPoints;
                    }
                }
                string captureKey = operation + ":" + phase;
                if (wwCaptureEvidence.ContainsKey(captureKey) || wwCaptureErrors.ContainsKey(captureKey)) return;
                string rawName; string watermarkedName; string capturedAt; string captureDetail;
                bool afterClaim = phase == "after";
                if (CaptureWwRewardEvidence(binding, staging, phase, afterClaim,
                        out rawName, out watermarkedName, out capturedAt, out captureDetail)) {
                    var captured = new List<Tuple<string, string, bool, string>> {
                        Tuple.Create(
                            rawName,
                            afterClaim ? "game-ui-daily-reward-raw" : "game-ui-daily-reward-before",
                            true,
                            capturedAt)
                    };
                    if (afterClaim) {
                        captured.Add(Tuple.Create(
                            watermarkedName,
                            "game-ui-daily-reward-watermarked",
                            false,
                            capturedAt));
                    }
                    wwCaptureEvidence[captureKey] = captured;
                } else {
                    wwCaptureErrors[captureKey] = captureDetail;
                }
            };
            Action<string, string, string> terminalTodo = (operation, state, stageDetail) => {
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
                if (binding.GameId == "WW" && operation == "claim-daily-reward") {
                    List<Tuple<string, string, bool, string>> beforeEvidence;
                    List<Tuple<string, string, bool, string>> afterEvidence;
                    bool hasBefore = wwCaptureEvidence.TryGetValue(operation + ":before", out beforeEvidence);
                    bool hasAfter = wwCaptureEvidence.TryGetValue(operation + ":after", out afterEvidence);
                    foreach (List<Tuple<string, string, bool, string>> captureSet in
                        new[] { beforeEvidence, afterEvidence }.Where(value => value != null)) {
                        foreach (Tuple<string, string, bool, string> artifact in captureSet) {
                            string imagePath = Path.Combine(staging, artifact.Item1);
                            string imageArtifactId = Guid.NewGuid().ToString();
                            Emit(Base(request, binding.GameId, "artifact_staged", sequence++, new Dictionary<string, object> {
                                { "todoInstanceId", id }, { "todoAttemptId", attemptId }, { "artifactId", imageArtifactId },
                                { "kind", artifact.Item2 }, { "fileName", artifact.Item1 }, { "mimeType", "image/png" },
                                { "sizeBytes", new FileInfo(imagePath).Length }, { "sha256", Hash(imagePath) }, { "capturedAt", artifact.Item4 }
                            }));
                            evidenceArtifactIds.Add(imageArtifactId);
                        }
                    }
                    if (state == "completed" && hasBefore && hasAfter && wwDailyActivityPoints >= 100) {
                        string pointsName = "ww-daily-activity-100-" + Guid.NewGuid().ToString("N") + ".txt";
                        string pointsPath = Path.Combine(staging, pointsName);
                        File.WriteAllText(pointsPath, "dailyActivityPoints=" + wwDailyActivityPoints, new UTF8Encoding(false));
                        string pointsArtifactId = Guid.NewGuid().ToString();
                        Emit(Base(request, binding.GameId, "artifact_staged", sequence++, new Dictionary<string, object> {
                            { "todoInstanceId", id }, { "todoAttemptId", attemptId }, { "artifactId", pointsArtifactId },
                            { "kind", "game-ui-daily-activity-100" }, { "fileName", pointsName }, { "mimeType", "text/plain" },
                            { "sizeBytes", new FileInfo(pointsPath).Length }, { "sha256", Hash(pointsPath) }, { "capturedAt", DateTime.UtcNow.ToString("o") }
                        }));
                        evidenceArtifactIds.Add(pointsArtifactId);
                        safeStageDetail += "; dailyActivityPoints=" + wwDailyActivityPoints
                            + "; completionScreenshot=before+after-raw+after-watermarked";
                    } else if (state == "completed") {
                        state = "failed";
                        string beforeError;
                        string afterError;
                        wwCaptureErrors.TryGetValue(operation + ":before", out beforeError);
                        wwCaptureErrors.TryGetValue(operation + ":after", out afterError);
                        safeStageDetail += "; completion_contract_failed: dailyActivityPoints="
                            + wwDailyActivityPoints + "; before="
                            + (hasBefore ? "ok" : (beforeError ?? "missing"))
                            + "; after=" + (hasAfter ? "ok" : (afterError ?? "missing"));
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
                bool failed = state == "failed";
                bool skipped = state == "skipped";
                bool completedStage = state == "completed";
                // A clean process exit is transport evidence only. Every
                // supported upstream tool must emit a structured stage event
                // before a selected Todo can be considered completed.
                // "not needed today" is a successful terminal outcome for a
                // selected conditional daily.  Preserve the upstream reason
                // and artifact, but do not leave the current-day obligation
                // permanently skipped/deferred.
                string terminalStatus = failed ? "failed" : (skipped || completedStage) ? "completed" : "review_required";
                string reasonCode = failed ? TypedFailureReason(safeStageDetail, "upstream_stage_failed") : skipped ? "upstream_stage_not_needed" : completedStage ? "upstream_stage_completed" : "upstream_stage_unverified";
                Emit(Base(request, binding.GameId, "todo_terminal", sequence++, new Dictionary<string, object> {
                    { "todoInstanceId", id }, { "todoAttemptId", attemptId }, { "status", terminalStatus },
                    { "reasonCode", reasonCode },
                    // An unverified routine stage is not completion, but a
                    // fresh full batch may retry the fixed GUI lifecycle from
                    // the beginning. Human-required outcomes are produced by
                    // the upstream stage bridge and remain non-retryable.
                    { "reason", safeStageDetail }, { "retryable", failed || (!skipped && !completedStage) }, { "evidenceArtifactIds", evidenceArtifactIds.ToArray() }
                }));
                terminal.Add(id);
                if (terminalStatus == "completed") completed.Add(id);
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
            int exitCode; string detail; bool cancelled;
            try {
                exitCode = RunDaily(binding, todosByOperation.Keys.ToArray(), (operation, state, stageDetail) => {
                    if (state == "started") startTodo(operation);
                    else if (state == "progress") progressTodo(operation, stageDetail);
                    else if (state == "capture_before") captureWwEvidence(operation, "before", stageDetail);
                    else if (state == "capture_after") captureWwEvidence(operation, "after", stageDetail);
                    else if (state == "completed" || state == "failed" || state == "skipped") terminalTodo(operation, state, stageDetail);
                }, () => cancellationRequested.WaitOne(0), out detail, out cancelled);
            }
            catch (Exception error) { exitCode = -1; detail = "tool_start_failed: " + error.Message; cancelled = false; }
            if (!cancelled) {
                foreach (string operation in todosByOperation.Keys) {
                    Tuple<string, IDictionary<string, object>> target = todosByOperation[operation];
                    if (attempted.Contains(target.Item1) && !terminal.Contains(target.Item1))
                        terminalTodo(operation, exitCode == 0 ? "review_required" : "failed", detail + "; stage_event_missing");
                }
            }
            // PyAppify's visible WW shell returns 70 after an --exit one-time
            // task even when its own log says "Script run Success".  The
            // run-scoped stage bridge is the authoritative semantic result;
            // accept that fixed wrapper code only when every selected Todo
            // emitted a completed terminal event.  Other non-zero exits stay
            // crashes and can never be hidden by this normalization.
            bool wwFormalExitCompleted = binding.GameId == "WW" && exitCode == 70 && completed.Count == ids.Length;
            bool transportClean = exitCode == 0 || wwFormalExitCompleted;
            bool allCompleted = completed.Count == ids.Length && transportClean;
            string runStatus = cancelled ? "cancelled" : (allCompleted ? "completed" : (transportClean ? "review_required" : "failed"));
            int declaredExitCode = wwFormalExitCompleted ? 0 : exitCode;
            string[] attemptedIds = ids.Cast<string>().Where(id => attempted.Contains(id)).ToArray();
            string[] completedIds = ids.Cast<string>().Where(id => completed.Contains(id)).ToArray();
            string[] unresolvedIds = ids.Cast<string>().Where(id => !completed.Contains(id)).ToArray();
            Emit(Base(request, binding.GameId, "run_terminal", sequence++, new Dictionary<string, object> {
                { "status", runStatus }, { "transportOutcome", cancelled ? "cancelled" : (transportClean ? "clean" : "crashed") },
                { "attemptedTodoInstanceIds", attemptedIds }, { "completedTodoInstanceIds", completedIds }, { "unresolvedTodoInstanceIds", unresolvedIds },
                { "terminalEventDigest", TerminalDigest(request, runStatus, attemptedIds, completedIds, unresolvedIds, declaredExitCode) }, { "exitCode", declaredExitCode }
            }));
            // The Host compares the process exit with run_terminal.  Returning
            // zero here used to replace a precise upstream failure with the
            // generic process_exit_mismatch error.
            return declaredExitCode;
        }

        private static bool CaptureWwRewardEvidence(Binding binding, string staging, string phase, bool includeWatermark,
            out string rawName, out string watermarkedName, out string capturedAt, out string detail) {
            rawName = null;
            watermarkedName = null;
            capturedAt = DateTime.UtcNow.ToString("o");
            detail = "ww_game_window_not_found";
            IntPtr window;
            NativeRect bounds;
            if (!FindWwGameWindow(binding, out window, out bounds, out detail)) return false;
            int width = bounds.Right - bounds.Left;
            int height = bounds.Bottom - bounds.Top;
            Bitmap raw = null;
            try {
                ShowWindowAsync(window, SwShow);
                SetForegroundWindow(window);
                Thread.Sleep(1200);
                raw = new Bitmap(width, height, PixelFormat.Format24bppRgb);
                bool printed;
                using (Graphics graphics = Graphics.FromImage(raw)) {
                    IntPtr hdc = graphics.GetHdc();
                    try { printed = PrintWindow(window, hdc, 2); }
                    finally { graphics.ReleaseHdc(hdc); }
                }
                if (!printed || !FrameHasVisualContent(raw)) {
                    using (Graphics graphics = Graphics.FromImage(raw)) {
                        graphics.CopyFromScreen(bounds.Left, bounds.Top, 0, 0, new Size(width, height), CopyPixelOperation.SourceCopy);
                    }
                }
                if (!FrameHasVisualContent(raw)) {
                    detail = "ww_game_capture_blank";
                    return false;
                }
                string token = Guid.NewGuid().ToString("N");
                rawName = "ww-reward-" + phase + "-raw-" + token + ".png";
                watermarkedName = includeWatermark ? "ww-reward-" + phase + "-watermarked-" + token + ".png" : null;
                string rawPath = Path.Combine(staging, rawName);
                raw.Save(rawPath, ImageFormat.Png);
                if (includeWatermark) {
                    string watermarkedPath = Path.Combine(staging, watermarkedName);
                    using (Bitmap marked = new Bitmap(raw))
                    using (Graphics graphics = Graphics.FromImage(marked)) {
                        graphics.SmoothingMode = SmoothingMode.AntiAlias;
                        graphics.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
                        string stamp = "YeYu Gamer | " + DateTime.UtcNow.AddHours(8).ToString("yyyy-MM-dd HH:mm:ss") + " CST (UTC+8)";
                        float fontSize = Math.Max(18f, Math.Min(32f, width / 55f));
                        using (Font font = MakeWatermarkFont(fontSize))
                        using (Brush foreground = new SolidBrush(Color.White))
                        using (Brush background = new SolidBrush(Color.FromArgb(190, 0, 0, 0))) {
                            SizeF textSize = graphics.MeasureString(stamp, font);
                            float padding = Math.Max(10f, fontSize * 0.55f);
                            RectangleF box = new RectangleF(
                                Math.Max(0f, width - textSize.Width - padding * 2f),
                                Math.Max(0f, height - textSize.Height - padding * 2f),
                                Math.Min(width, textSize.Width + padding * 2f),
                                Math.Min(height, textSize.Height + padding * 2f));
                            graphics.FillRectangle(background, box);
                            graphics.DrawString(stamp, font, foreground, box.Left + padding, box.Top + padding);
                        }
                        marked.Save(watermarkedPath, ImageFormat.Png);
                    }
                }
                capturedAt = DateTime.UtcNow.ToString("o");
                detail = "wwGameWindow=" + width + "x" + height + "; phase=" + phase
                    + (includeWatermark ? "; watermark=Beijing-time" : String.Empty);
                return true;
            } catch (Exception error) {
                detail = "ww_game_capture_error: " + error.GetType().Name + ": " + error.Message;
                if (rawName != null) TryDelete(Path.Combine(staging, rawName));
                if (watermarkedName != null) TryDelete(Path.Combine(staging, watermarkedName));
                rawName = null;
                watermarkedName = null;
                return false;
            } finally {
                if (raw != null) raw.Dispose();
            }
        }

        private static bool CaptureLaunchTimeoutEvidence(Binding binding, string staging,
            out string fileName, out string capturedAt, out string detail) {
            fileName = null;
            capturedAt = DateTime.UtcNow.ToString("o");
            detail = "launch_timeout_window_not_found";
            if (String.IsNullOrEmpty(staging)) return false;
            IntPtr window;
            NativeRect bounds;
            string windowDetail;
            if (!FindLaunchTimeoutWindow(binding, out window, out bounds, out windowDetail)) {
                detail = windowDetail;
                return false;
            }
            return CaptureWindowPng(window, bounds, staging,
                binding.GameId.ToLowerInvariant() + "-launch-timeout",
                out fileName, out capturedAt, out detail);
        }

        private static bool FindLaunchTimeoutWindow(Binding binding, out IntPtr window, out NativeRect bounds, out string detail) {
            window = IntPtr.Zero;
            bounds = new NativeRect();
            detail = "launch_timeout_window_not_found";
            if (FindWindowForExecutable(binding.Executable, binding.ToolRoot, false, new string[0],
                    "formal_gui_window", out window, out bounds, out detail)) return true;
            if (!String.IsNullOrEmpty(binding.InnerPython) &&
                FindWindowForExecutable(binding.InnerPython, binding.ToolRoot, true, new string[0],
                    "formal_inner_gui_window", out window, out bounds, out detail)) return true;
            string gameRoot = String.IsNullOrEmpty(binding.Game) ? null : Path.GetDirectoryName(Path.GetFullPath(binding.Game));
            string[] extraNames = binding.GameId == "WW" ? new string[] { "Client-Win64-Shipping" } : new string[0];
            return FindWindowForExecutable(binding.Game, gameRoot, true, extraNames,
                "game_window", out window, out bounds, out detail);
        }

        private static bool FindWindowForExecutable(string executable, string allowedRoot, bool allowRootDescendant,
            string[] extraProcessNames, string label, out IntPtr window, out NativeRect bounds, out string detail) {
            window = IntPtr.Zero;
            bounds = new NativeRect();
            detail = label + "_not_found";
            if (String.IsNullOrEmpty(executable)) return false;
            string expected = Path.GetFullPath(executable);
            string root = String.IsNullOrEmpty(allowedRoot) ? null :
                Path.GetFullPath(allowedRoot).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            names.Add(Path.GetFileNameWithoutExtension(executable));
            if (extraProcessNames != null) foreach (string name in extraProcessNames) if (!String.IsNullOrWhiteSpace(name)) names.Add(name);
            foreach (string processName in names) {
                foreach (Process process in Process.GetProcessesByName(processName)) {
                    try {
                        process.Refresh();
                        IntPtr candidate = process.MainWindowHandle;
                        if (candidate == IntPtr.Zero || !IsWindowVisible(candidate)) continue;
                        string image = null;
                        try { image = process.MainModule.FileName; }
                        catch (System.ComponentModel.Win32Exception) { }
                        catch (InvalidOperationException) { }
                        if (!String.IsNullOrEmpty(image)) {
                            string fullImage = Path.GetFullPath(image);
                            if (!String.Equals(fullImage, expected, StringComparison.OrdinalIgnoreCase) &&
                                !(allowRootDescendant && root != null && fullImage.StartsWith(root, StringComparison.OrdinalIgnoreCase)))
                                continue;
                        }
                        NativeRect rect;
                        if (!GetWindowRect(candidate, out rect)) continue;
                        int width = rect.Right - rect.Left;
                        int height = rect.Bottom - rect.Top;
                        if (width < 320 || height < 180) continue;
                        window = candidate;
                        bounds = rect;
                        detail = label + "=" + width + "x" + height;
                        return true;
                    } finally { process.Dispose(); }
                }
            }
            return false;
        }

        private static bool CaptureWindowPng(IntPtr window, NativeRect bounds, string staging, string prefix,
            out string fileName, out string capturedAt, out string detail) {
            fileName = null;
            capturedAt = DateTime.UtcNow.ToString("o");
            detail = prefix + "_capture_failed";
            int width = bounds.Right - bounds.Left;
            int height = bounds.Bottom - bounds.Top;
            Bitmap raw = null;
            try {
                Directory.CreateDirectory(staging);
                ShowWindowAsync(window, SwShow);
                SetForegroundWindow(window);
                Thread.Sleep(500);
                raw = new Bitmap(width, height, PixelFormat.Format24bppRgb);
                bool printed;
                using (Graphics graphics = Graphics.FromImage(raw)) {
                    IntPtr hdc = graphics.GetHdc();
                    try { printed = PrintWindow(window, hdc, 2); }
                    finally { graphics.ReleaseHdc(hdc); }
                }
                if (!printed || !FrameHasVisualContent(raw)) {
                    using (Graphics graphics = Graphics.FromImage(raw)) {
                        graphics.CopyFromScreen(bounds.Left, bounds.Top, 0, 0, new Size(width, height), CopyPixelOperation.SourceCopy);
                    }
                }
                if (!FrameHasVisualContent(raw)) {
                    detail = prefix + "_capture_blank";
                    return false;
                }
                fileName = prefix + "-" + Guid.NewGuid().ToString("N") + ".png";
                raw.Save(Path.Combine(staging, fileName), ImageFormat.Png);
                capturedAt = DateTime.UtcNow.ToString("o");
                detail = prefix + "_captured=" + width + "x" + height;
                return true;
            } catch (Exception error) {
                detail = prefix + "_capture_error: " + error.GetType().Name + ": " + error.Message;
                if (fileName != null) TryDelete(Path.Combine(staging, fileName));
                fileName = null;
                return false;
            } finally {
                if (raw != null) raw.Dispose();
            }
        }

        private static bool FindWwGameWindow(Binding binding, out IntPtr window, out NativeRect bounds, out string detail) {
            window = IntPtr.Zero;
            bounds = new NativeRect();
            detail = "ww_game_window_not_found";
            string expectedImage = Path.GetFullPath(binding.Game);
            string expectedRoot = Path.GetDirectoryName(expectedImage).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            string[] allowedNames = { Path.GetFileNameWithoutExtension(expectedImage), "Client-Win64-Shipping" };
            foreach (string processName in allowedNames.Distinct(StringComparer.OrdinalIgnoreCase)) {
                foreach (Process process in Process.GetProcessesByName(processName)) {
                    try {
                        process.Refresh();
                        IntPtr candidate = process.MainWindowHandle;
                        if (candidate == IntPtr.Zero || !IsWindowVisible(candidate)) continue;
                        string image = null;
                        try { image = process.MainModule.FileName; }
                        catch (System.ComponentModel.Win32Exception) { }
                        catch (InvalidOperationException) { }
                        if (!String.IsNullOrEmpty(image)) {
                            string fullImage = Path.GetFullPath(image);
                            if (!String.Equals(fullImage, expectedImage, StringComparison.OrdinalIgnoreCase) &&
                                !fullImage.StartsWith(expectedRoot, StringComparison.OrdinalIgnoreCase)) continue;
                        }
                        NativeRect rect;
                        if (!GetWindowRect(candidate, out rect)) continue;
                        int width = rect.Right - rect.Left;
                        int height = rect.Bottom - rect.Top;
                        if (width < 640 || height < 360) continue;
                        window = candidate;
                        bounds = rect;
                        detail = "ww_game_window_ready";
                        return true;
                    } finally { process.Dispose(); }
                }
            }
            return false;
        }

        private static bool FrameHasVisualContent(Bitmap bitmap) {
            int minimum = 255;
            int maximum = 0;
            var colors = new HashSet<int>();
            for (int y = bitmap.Height / 16; y < bitmap.Height; y += Math.Max(1, bitmap.Height / 12)) {
                for (int x = bitmap.Width / 16; x < bitmap.Width; x += Math.Max(1, bitmap.Width / 16)) {
                    Color color = bitmap.GetPixel(Math.Min(bitmap.Width - 1, x), Math.Min(bitmap.Height - 1, y));
                    int brightness = (color.R + color.G + color.B) / 3;
                    minimum = Math.Min(minimum, brightness);
                    maximum = Math.Max(maximum, brightness);
                    colors.Add(color.ToArgb());
                }
            }
            return maximum - minimum >= 18 && colors.Count >= 12;
        }

        private static Font MakeWatermarkFont(float size) {
            try { return new Font("Microsoft YaHei UI", size, FontStyle.Bold, GraphicsUnit.Pixel); }
            catch { return new Font(FontFamily.GenericSansSerif, size, FontStyle.Bold, GraphicsUnit.Pixel); }
        }

        private static void TryDelete(string path) { try { if (File.Exists(path)) File.Delete(path); } catch { } }

        private static Dictionary<string, string> ParseArguments(string[] args) {
            if (args == null || args.Length != 8) return null;
            var result = new Dictionary<string, string>(StringComparer.Ordinal);
            for (int i = 0; i < args.Length; i += 2) { if (!args[i].StartsWith("--") || result.ContainsKey(args[i])) return null; result.Add(args[i], args[i + 1]); }
            return result.Count == 4 && result.ContainsKey("--protocol-version") && result.ContainsKey("--run-id") && result.ContainsKey("--run-attempt-id") && result.ContainsKey("--game-id") ? result : null;
        }
        private static bool SupportedGame(string gameId) { return gameId == "WW" || gameId == "Endfield" || gameId == "GF2"; }
        private static bool MatchesRequest(IDictionary<string, object> r, Dictionary<string, string> o) {
            return r != null && (r["protocolVersion"] as string) == ProtocolVersion && (r["gameId"] as string) == o["--game-id"] &&
                (r["runId"] as string) == o["--run-id"] && (r["runAttemptId"] as string) == o["--run-attempt-id"] && r["preserveClientOnStop"] is bool && (bool)r["preserveClientOnStop"];
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
            return operation == "attach-home" || operation == "mail" || operation == "public-area-dispatch" || operation == "spend-stamina" || operation == "squad-tasks" || operation == "claim-daily-missions" || operation == "battle-pass-free-track";
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
            bool validVersionTwo = versionTwo && expectedGameId == "WW" && doc.Count == 5 && doc["dailyTaskProfile"] is IDictionary<string, object>;
            if (doc == null || (!validVersionOne && !validVersionTwo) || (doc["gameId"] as string) != expectedGameId || !(doc["gamePath"] is string) || !(doc["toolPath"] is string)) throw new InvalidOperationException("manager_installation_binding_invalid");
            string game = doc["gamePath"] as string; string root = doc["toolPath"] as string;
            if (!Path.IsPathRooted(game) || !Path.IsPathRooted(root) || game.StartsWith("\\\\") || root.StartsWith("\\\\")) throw new InvalidOperationException("configured_installation_missing");
            return MakeFormalGuiBinding(expectedGameId, game, root,
                doc.ContainsKey("dailyTaskProfile") ? doc["dailyTaskProfile"] as IDictionary<string, object> : null);
        }
        private static Binding MakeFormalGuiBinding(string gameId, string game, string root, IDictionary<string, object> dailyTaskProfile) {
            if (gameId == "WW") {
                string working = Path.Combine(root, "data", "apps", "ok-ww", "working");
                Binding binding = MakeBinding(gameId, game, root, Path.Combine(root, "ok-ww.exe"),
                    String.Empty, Path.Combine(working, "configs", "DailyTask.json"), dailyTaskProfile, root);
                binding.AppName = "ok-ww";
                binding.UpdateState = Path.Combine(root, "data", "apps", "ok-ww", "app.json");
                binding.InnerPython = Path.Combine(root, "data", "apps", "ok-ww", "python", "pythonw.exe");
                binding.InnerEntry = Path.Combine(working, "main.py");
                if (!File.Exists(binding.UpdateState) || !File.Exists(binding.InnerPython) || !File.Exists(binding.InnerEntry)) throw new InvalidOperationException("configured_gui_entry_missing");
                return binding;
            }
            if (gameId == "Endfield") {
                string appRoot = Path.Combine(root, "data", "apps", "ok-ef");
                string endfieldWorkingDirectory = Path.Combine(appRoot, "working");
                Binding binding = MakeBinding(gameId, game, root, Path.Combine(root, "ok-ef.exe"),
                    String.Empty, Path.Combine(endfieldWorkingDirectory, "configs", "DailyTask.json"), null, root);
                binding.AppName = "ok-ef";
                binding.UpdateState = Path.Combine(appRoot, "app.json");
                binding.InnerPython = Path.Combine(appRoot, "python", "pythonw.exe");
                binding.InnerEntry = Path.Combine(endfieldWorkingDirectory, "main.py");
                if (!File.Exists(binding.UpdateState) || !File.Exists(binding.InnerPython) || !File.Exists(binding.InnerEntry)) throw new InvalidOperationException("configured_gui_entry_missing");
                return binding;
            }
            string workingDirectory = Path.Combine(root, "working");
            string formalEntry = EnsureGf2FormalEntry(root, workingDirectory);
            return MakeBinding(gameId, game, root, formalEntry,
                "-t 1 -e", Path.Combine(workingDirectory, "configs", "DailyTask.json"), null, root);
        }
        private static string EnsureGf2FormalEntry(string root, string workingDirectory) {
            string formalEntry = Path.Combine(root, "ok-gf2.exe");
            string packagedEntry = Path.Combine(workingDirectory, "ok-gf2.exe");
            string updateState = Path.Combine(root, "app.json");
            if (!File.Exists(packagedEntry) || !File.Exists(updateState))
                throw new InvalidOperationException("configured_gui_entry_missing");
            // PyAppify resolves app.json beside its signed bootstrap EXE.  Some
            // migrated installations retained only the repository copy under
            // working/, which opens a generic Error window before its updater
            // can run. Restore the fixed formal root entry atomically once;
            // future formal updates own and replace this root copy themselves.
            if (!File.Exists(formalEntry)) {
                string temporary = formalEntry + "." + Guid.NewGuid().ToString("N") + ".tmp";
                File.Copy(packagedEntry, temporary, true);
                File.Move(temporary, formalEntry);
                if (!File.Exists(formalEntry) || new FileInfo(formalEntry).Length != new FileInfo(packagedEntry).Length)
                    throw new InvalidOperationException("formal_gui_entry_restore_failed");
            }
            NormalizeGf2Profile(updateState);
            return formalEntry;
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
            if (String.IsNullOrEmpty(game) || String.IsNullOrEmpty(toolRoot) || !File.Exists(game) || !File.Exists(executable) || !File.Exists(dailyConfig) || !Directory.Exists(effectiveWorkingDirectory)) throw new InvalidOperationException("configured_installation_missing");
            if (gameId == "GF2" && !File.Exists(Path.Combine(toolRoot, "working", "main.py"))) throw new InvalidOperationException("configured_gui_entry_missing");
            Binding result = new Binding { GameId = gameId, Game = game, ToolRoot = toolRoot, WorkingDirectory = effectiveWorkingDirectory, Executable = executable, Arguments = arguments, DailyConfig = dailyConfig, DailyTaskProfile = dailyTaskProfile };
            if (gameId == "GF2") {
                result.AppName = "ok-gf2";
                result.UpdateState = Path.Combine(toolRoot, "app.json");
                result.InnerPython = Path.Combine(toolRoot, "python", "pythonw.exe");
                result.InnerEntry = Path.Combine(toolRoot, "working", "main.py");
                if (!File.Exists(result.UpdateState) || !File.Exists(result.InnerPython) || !File.Exists(result.InnerEntry))
                    throw new InvalidOperationException("configured_gui_entry_missing");
            }
            return result;
        }
        private static bool NumberEquals(object value, int expected) { return (value is int && (int)value == expected) || (value is decimal && (decimal)value == expected); }
        private static int RunDaily(Binding binding, string[] selectedOperations, Action<string, string, string> onStage, Func<bool> cancellationRequested, out string detail, out bool cancelled) {
            cancelled = false;
            if (binding.GameId == "WW" && binding.DailyTaskProfile != null) ApplyOkWWDailyTaskProfile(binding);
            bool shellLaunchedFormal = binding.GameId == "WW" || binding.GameId == "Endfield" || binding.GameId == "GF2";
            bool useShellExecute = shellLaunchedFormal;
            if (shellLaunchedFormal) StopStaleFormalProcesses(binding);
            var start = new ProcessStartInfo { FileName = binding.Executable, Arguments = binding.Arguments, WorkingDirectory = binding.WorkingDirectory, UseShellExecute = useShellExecute, CreateNoWindow = false };
            string staging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            string stageFile = Path.Combine(staging, binding.GameId.ToLowerInvariant() + "-stage-" + Guid.NewGuid().ToString("N") + ".jsonl");
            File.WriteAllText(stageFile, String.Empty, new UTF8Encoding(false));
            var seenStageLines = new HashSet<string>(StringComparer.Ordinal);
            DateTime formalStartedAtUtc = DateTime.UtcNow;
            if (shellLaunchedFormal) {
                // Endfield's formal updater can replace the working tree before
                // auto-starting Python. Hold auto-start until that update pass is
                // complete, then inject the run bridge and reopen the same formal
                // GUI entry. This keeps both updating and execution visible.
                if (binding.GameId == "Endfield") SetFormalAutoStart(binding, false);
                WriteFormalRunSidecar(binding, stageFile, selectedOperations);
            }
            else {
                start.EnvironmentVariables["YEYU_GAMER_STAGE_FILE"] = stageFile;
                start.EnvironmentVariables["YEYU_GAMER_SELECTED_OPERATIONS"] = Json.Serialize(selectedOperations);
                ConfigureToolOutput(start);
            }
            try {
                var process = Process.Start(start);
                if (!shellLaunchedFormal) DrainToolOutput(process);
                Process formalProcess = null;
                string attachOperation = binding.GameId == "GF2" ? "attach-home" : "attach-world";
                // Each fixed entry starts the upstream Qt GUI and selects its
                // first one-time DailyTask. The GUI remains visible while its
                // own updater/startup path runs and emits finer-grained stages.
                onStage(attachOperation, "started", binding.GameId + " formal GUI launched; waiting for its DailyTask runtime");
                string formalHandoff = String.Empty;
                if (shellLaunchedFormal) {
                    formalProcess = process;
                    int handoffExit;
                    Process inner;
                    if (!WaitForFormalUpdateAndStartInner(binding, ref formalProcess, stageFile, selectedOperations,
                        formalStartedAtUtc, cancellationRequested, onStage, out inner, out handoffExit,
                        out formalHandoff, out cancelled)) {
                        detail = formalHandoff;
                        return handoffExit;
                    }
                    process = inner;
                    // Discovering Python/Qt proves only transport handoff.  The
                    // run-scoped bridge must emit the semantic attach terminal.
                }
                while (!process.WaitForExit(150)) {
                    process.Refresh();
                    if (binding.GameId == "GF2" && String.Equals(process.MainWindowTitle, "Error", StringComparison.OrdinalIgnoreCase)) {
                        string blockingState = DetectFormalBlockingState(binding, process);
                        StopToolProcess(process);
                        detail = String.IsNullOrEmpty(blockingState)
                            ? "client_update_required: GF2 formal GUI stopped at an error gate"
                            : blockingState;
                        return 72;
                    }
                    if (cancellationRequested != null && cancellationRequested()) {
                        // This stops only the fixed tool child. The game client is
                        // intentionally not part of the process tree we terminate.
                        StopToolProcess(process);
                        StopToolProcess(formalProcess);
                        detail = "cancelled_by_manager";
                        cancelled = true;
                        return process.HasExited ? process.ExitCode : -1;
                    }
                    DrainOkWWStageEvents(stageFile, seenStageLines, onStage);
                }
                DrainOkWWStageEvents(stageFile, seenStageLines, onStage);
                StopToolProcess(formalProcess);
                int toolExitCode = SafeExitCode(process);
                if (seenStageLines.Count == 0) {
                    detail = "telemetry_missing: fixed DailyTask emitted no run-scoped stage event";
                    return 73;
                }
                detail = "fixedTask=daily; gameId=" + binding.GameId + "; toolExitCode=" + toolExitCode +
                    (String.IsNullOrEmpty(formalHandoff) ? String.Empty : "; " + formalHandoff); return toolExitCode;
            } finally {
                if (binding.GameId == "Endfield") {
                    try { SetFormalAutoStart(binding, true); }
                    catch (Exception) { }
                }
                if (shellLaunchedFormal) ClearFormalRunSidecars(binding);
            }
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
        private sealed class FormalUpdatePolicy {
            public int CheckSeconds;
            public int IdleSeconds;
            public int HardCapSeconds;
        }
        private sealed class FormalUpdateObservation {
            public bool Exists;
            public bool ValidJson;
            public bool Installed;
            public string CurrentVersion = String.Empty;
            public string StartingVersion = String.Empty;
            public string UpdateState = String.Empty;
            public string UpdateTargetVersion = String.Empty;
            public string UpdateError = String.Empty;
            public DateTime LastWriteUtc = DateTime.MinValue;
            public long Length;
            public string ContentDigest = String.Empty;
        }
        private static FormalUpdatePolicy FormalUpdatePolicyFromEnvironment() {
            int check = PositiveEnvInt("YEYU_GAMER_FORMAL_UPDATE_CHECK_SECONDS", 120, 15, 3600);
            int idle = PositiveEnvInt("YEYU_GAMER_FORMAL_UPDATE_IDLE_SECONDS", check, 15, 3600);
            int hard = PositiveEnvInt("YEYU_GAMER_FORMAL_UPDATE_HARD_CAP_SECONDS", 1200, 60, 86400);
            if (hard < check) hard = check;
            if (hard < idle) hard = idle;
            return new FormalUpdatePolicy { CheckSeconds = check, IdleSeconds = idle, HardCapSeconds = hard };
        }
        private static int PositiveEnvInt(string name, int defaultValue, int minimum, int maximum) {
            string raw = Environment.GetEnvironmentVariable(name);
            int value;
            if (String.IsNullOrWhiteSpace(raw) || !Int32.TryParse(raw.Trim(), out value) || value < minimum || value > maximum)
                return defaultValue;
            return value;
        }
        private static FormalUpdateObservation ReadFormalUpdateObservation(string path) {
            var observation = new FormalUpdateObservation();
            try {
                if (String.IsNullOrEmpty(path) || !File.Exists(path)) return observation;
                FileInfo info = new FileInfo(path);
                string content;
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                using (var reader = new StreamReader(stream, Encoding.UTF8, true)) content = reader.ReadToEnd();
                observation.Exists = true;
                observation.LastWriteUtc = info.LastWriteTimeUtc;
                observation.Length = info.Length;
                using (var sha = SHA256.Create()) {
                    observation.ContentDigest = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(content))).Replace("-", String.Empty).ToLowerInvariant();
                }
                IDictionary<string, object> state = Json.DeserializeObject(content) as IDictionary<string, object>;
                if (state == null) return observation;
                observation.ValidJson = true;
                object installed;
                observation.Installed = state.TryGetValue("installed", out installed) && installed is bool && (bool)installed;
                observation.CurrentVersion = JsonText(state, "current_version");
                observation.StartingVersion = JsonText(state, "app_starting_version");
                observation.UpdateState = JsonText(state, "update_state");
                observation.UpdateTargetVersion = JsonText(state, "update_target_version");
                observation.UpdateError = JsonText(state, "update_error");
            } catch (Exception) { }
            return observation;
        }
        private static string JsonText(IDictionary<string, object> state, string key) {
            object value;
            if (state == null || !state.TryGetValue(key, out value) || value == null) return String.Empty;
            return Convert.ToString(value, System.Globalization.CultureInfo.InvariantCulture) ?? String.Empty;
        }
        private static bool FormalObservationChanged(FormalUpdateObservation current, FormalUpdateObservation previous) {
            if (current == null || previous == null) return false;
            return current.Exists != previous.Exists ||
                current.LastWriteUtc != previous.LastWriteUtc ||
                current.Length != previous.Length ||
                !String.Equals(current.ContentDigest, previous.ContentDigest, StringComparison.Ordinal);
        }
        private static bool FormalObservationShowsProgress(FormalUpdateObservation current, FormalUpdateObservation previous) {
            if (current == null || !current.Exists) return false;
            if (!String.IsNullOrWhiteSpace(current.UpdateTargetVersion)) return true;
            if (!String.IsNullOrWhiteSpace(current.UpdateState) &&
                !String.Equals(current.UpdateState, "idle", StringComparison.OrdinalIgnoreCase)) return true;
            return previous != null && FormalObservationChanged(current, previous);
        }
        private static string FormalUpdateSummary(FormalUpdateObservation observation, DateTime startedAtUtc, DateTime nowUtc, string extra) {
            string state = observation == null || String.IsNullOrWhiteSpace(observation.UpdateState) ? "<missing>" : observation.UpdateState;
            string target = observation == null || String.IsNullOrWhiteSpace(observation.UpdateTargetVersion) ? "<none>" : observation.UpdateTargetVersion;
            string error = observation == null || String.IsNullOrWhiteSpace(observation.UpdateError) ? "<none>" : observation.UpdateError;
            int elapsed = Math.Max(0, (int)(nowUtc - startedAtUtc).TotalSeconds);
            return "update_state=" + state + "; update_target_version=" + target +
                "; update_error=" + error + "; elapsedSeconds=" + elapsed +
                (String.IsNullOrWhiteSpace(extra) ? String.Empty : "; " + extra);
        }
        private static bool SupportsFormalUpdateRetry(string gameId) {
            return gameId == "WW" || gameId == "Endfield" || gameId == "GF2";
        }
        private static bool WaitForFormalUpdateAndStartInner(Binding binding, ref Process formal,
            string stageFile, string[] selectedOperations, DateTime formalStartedAtUtc,
            Func<bool> cancellationRequested, Action<string, string, string> onStage, out Process inner,
            out int exitCode, out string detail, out bool cancelled) {
            inner = null; exitCode = -1; detail = "telemetry_missing:formal_gui_update_check_failed"; cancelled = false;
            int formalUpdateAttempt = 1;
            FormalUpdatePolicy policy = FormalUpdatePolicyFromEnvironment();
            DateTime nextGf2StartAttempt = DateTime.MinValue;
            while (formalUpdateAttempt <= 2) {
                DateTime attemptStartedAtUtc = DateTime.UtcNow;
                int baseWindowSeconds = formalUpdateAttempt == 1 ? policy.CheckSeconds : 180;
                DateTime hardDeadline = attemptStartedAtUtc.AddSeconds(policy.HardCapSeconds);
                DateTime deadline = attemptStartedAtUtc.AddSeconds(baseWindowSeconds);
                if (deadline > hardDeadline) deadline = hardDeadline;
                DateTime nextUiGateCheck = DateTime.MinValue;
                FormalUpdateObservation previousObservation = null;
                FormalUpdateObservation lastObservation = null;
                DateTime lastProgressAtUtc = attemptStartedAtUtc;
                while (DateTime.UtcNow < deadline) {
                    if (cancellationRequested != null && cancellationRequested()) {
                        StopToolProcess(formal);
                        detail = formalUpdateAttempt == 1
                            ? "cancelled_by_manager_during_formal_update"
                            : "cancelled_by_manager_during_formal_update_retry";
                        cancelled = true;
                        exitCode = formal.HasExited ? formal.ExitCode : -1;
                        return false;
                    }
                    formal.WaitForExit(150);
                    RefreshFormalLayout(binding);
                    FormalUpdateObservation observation = ReadFormalUpdateObservation(binding.UpdateState);
                    lastObservation = observation;
                    DateTime nowUtc = DateTime.UtcNow;
                    if (nowUtc >= nextUiGateCheck) {
                        nextUiGateCheck = nowUtc.AddSeconds(2);
                        string blockingState = DetectFormalBlockingState(binding, formal);
                        if (!String.IsNullOrEmpty(blockingState)) {
                            StopToolProcess(formal);
                            detail = blockingState;
                            exitCode = 74;
                            return false;
                        }
                    }
                    if (FormalObservationShowsProgress(observation, previousObservation)) {
                        lastProgressAtUtc = nowUtc;
                        DateTime proposed = nowUtc.AddSeconds(policy.IdleSeconds);
                        if (proposed > hardDeadline) proposed = hardDeadline;
                        if (proposed > deadline.AddSeconds(15) ||
                            (deadline <= nowUtc.AddSeconds(5) && proposed > deadline)) {
                            deadline = proposed;
                            string progressDetail = "formal_gui_update_deadline_extended: " +
                                FormalUpdateSummary(observation, formalStartedAtUtc, nowUtc,
                                    "attempt=" + formalUpdateAttempt + "; deadlineUtc=" + deadline.ToString("o") +
                                    "; idleWindowSeconds=" + policy.IdleSeconds +
                                    "; hardCapSeconds=" + policy.HardCapSeconds);
                            if (onStage != null) {
                                foreach (string operation in selectedOperations)
                                    onStage(operation, "progress", progressDetail);
                            }
                        }
                    }
                    string updateError = String.Empty;
                    if (!FormalUpdateReady(observation, out updateError)) {
                        if (!String.IsNullOrEmpty(updateError)) {
                            StopToolProcess(formal);
                            detail = "client_update_required: " + updateError + "; " +
                                FormalUpdateSummary(observation, formalStartedAtUtc, nowUtc,
                                    "attempt=" + formalUpdateAttempt);
                            exitCode = -1;
                            return false;
                        }
                        previousObservation = observation;
                        continue;
                    }
                    Process preInjectionInner = null;
                    bool formalLoaded = HasFormalLoaded(binding, formalStartedAtUtc);
                    if (!formalLoaded && binding.GameId == "GF2") {
                        preInjectionInner = FindFormalInner(binding, formalStartedAtUtc);
                        formalLoaded = preInjectionInner != null;
                        if (!formalLoaded && DateTime.UtcNow >= nextGf2StartAttempt) {
                            TryStartGf2FormalApplication(binding, formal);
                            nextGf2StartAttempt = DateTime.UtcNow.AddSeconds(5);
                        }
                    }
                    if (!formalLoaded) {
                        previousObservation = observation;
                        Thread.Sleep(150);
                        continue;
                    }
                    if (preInjectionInner == null) preInjectionInner = FindFormalInner(binding, formalStartedAtUtc);
                    if (preInjectionInner != null) StopToolProcess(preInjectionInner);
                    if (binding.GameId == "WW") {
                        EnsureWwGuiStreamGuard(binding);
                        EnsureWwConditionalStageBoundaries(binding);
                    }
                    if (binding.GameId == "Endfield") EnsureEndfieldManagerBridge(binding);
                    EnsureFormalGuiManagerBridge(binding);
                    WriteFormalRunSidecar(binding, stageFile, selectedOperations);
                    DateTime innerStartedAfterUtc = formalStartedAtUtc;
                    if (binding.GameId == "Endfield" || binding.GameId == "GF2") {
                        if (binding.GameId == "Endfield") SetFormalAutoStart(binding, true);
                        StopToolProcess(formal);
                        innerStartedAfterUtc = DateTime.UtcNow;
                        var restart = new ProcessStartInfo {
                            FileName = binding.Executable,
                            Arguments = binding.Arguments,
                            WorkingDirectory = binding.WorkingDirectory,
                            UseShellExecute = true,
                            CreateNoWindow = false
                        };
                        formal = Process.Start(restart);
                        if (formal == null) throw new InvalidOperationException("formal_gui_restart_failed");
                    }
                    DateTime innerDeadline = DateTime.UtcNow.AddSeconds(180);
                    while (DateTime.UtcNow < innerDeadline) {
                        if (cancellationRequested != null && cancellationRequested()) {
                            StopToolProcess(formal);
                            detail = formalUpdateAttempt == 1
                                ? "cancelled_by_manager_during_inner_gui_startup"
                                : "cancelled_by_manager_during_inner_gui_retry_startup";
                            cancelled = true;
                            exitCode = formal.HasExited ? formal.ExitCode : -1;
                            return false;
                        }
                        inner = FindFormalInner(binding, innerStartedAfterUtc);
                        if (inner != null) {
                            detail = formalUpdateAttempt == 1
                                ? "formalGuiUpdate=checked; formalGuiAutoStart=true; innerGuiBound=true"
                                : "formalGuiUpdate=checked-after-retry; formalGuiAutoStart=true; innerGuiBound=true";
                            return true;
                        }
                        Thread.Sleep(150);
                    }
                    StopToolProcess(formal);
                    detail = formalUpdateAttempt == 1
                        ? "telemetry_missing:formal_gui_inner_bind_timeout"
                        : "telemetry_missing:formal_gui_inner_bind_retry_timeout";
                    return false;
                }
                // A PyAppify/libgit2 fetch can occasionally stop making progress
                // even though a fresh formal launch succeeds immediately. Retry
                // the same visible, update-capable entry once instead of skipping
                // the updater or holding the Manager batch for hours.
                if (formalUpdateAttempt == 1 && SupportsFormalUpdateRetry(binding.GameId)) {
                    StopToolProcess(formal);
                    StopStaleFormalProcesses(binding);
                    Thread.Sleep(500);
                    var retryStart = new ProcessStartInfo {
                        FileName = binding.Executable,
                        Arguments = binding.Arguments,
                        WorkingDirectory = binding.WorkingDirectory,
                        UseShellExecute = true,
                        CreateNoWindow = false
                    };
                    formalStartedAtUtc = DateTime.UtcNow;
                    formal = Process.Start(retryStart);
                    if (formal == null) {
                        detail = "formal_gui_update_retry_start_failed";
                        return false;
                    }
                    formalUpdateAttempt++;
                    nextGf2StartAttempt = DateTime.MinValue;
                    if (onStage != null) {
                        string retryDetail = "formal_gui_update_retry_started: " +
                            FormalUpdateSummary(lastObservation, formalStartedAtUtc, DateTime.UtcNow,
                                "retryWindowSeconds=180");
                        foreach (string operation in selectedOperations)
                            onStage(operation, "progress", retryDetail);
                    }
                    continue;
                }
                string screenshotName; string capturedAt; string captureDetail;
                string timeoutExtra = "attempt=" + formalUpdateAttempt +
                    "; idleWindowSeconds=" + policy.IdleSeconds +
                    "; hardCapSeconds=" + policy.HardCapSeconds +
                    "; lastProgressElapsedSeconds=" + Math.Max(0, (int)(lastProgressAtUtc - formalStartedAtUtc).TotalSeconds);
                if (CaptureLaunchTimeoutEvidence(binding, Path.GetDirectoryName(stageFile), out screenshotName, out capturedAt, out captureDetail)) {
                    timeoutExtra += "; launchTimeoutArtifact=" + screenshotName +
                        "; launchTimeoutArtifactKind=game-ui-launch-timeout; launchTimeoutCapturedAt=" + capturedAt;
                } else {
                    timeoutExtra += "; launchTimeoutCapture=" + captureDetail;
                }
                StopToolProcess(formal);
                detail = "telemetry_missing:formal_gui_update_check_timeout; " +
                    FormalUpdateSummary(lastObservation, formalStartedAtUtc, DateTime.UtcNow, timeoutExtra);
                exitCode = -1;
                return false;
            }
            detail = "telemetry_missing:formal_gui_update_check_timeout";
            exitCode = -1;
            return false;
        }
        private static string PatchWwConditionalStageBoundaries(string source) {
            const string marker = "# YEYU_GAMER_WW_CONDITIONAL_STAGES_V1";
            const string nightmareAnchor = "        manager_requires_nightmare = (\n";
            const string staminaAnchor = "        need_stamina = not daily_reward_ready and used_stamina < 180\n";
            const string nightmareAction = "'farm-nightmare-daily-echo', 'started',\n";
            const string staminaAction = "self.yeyu_stage('spend-waveplates', 'started', 'running configured stamina route')";
            string normalized = source.Replace("\r\n", "\n");
            string nightmareEntry =
                "        " + marker + "\n" +
                "        if (self._yeyu_selected_operations is not None\n" +
                "                and self.yeyu_operation_enabled('farm-nightmare-daily-echo')):\n" +
                "            self.yeyu_stage('farm-nightmare-daily-echo', 'started',\n" +
                "                            'evaluating current daily condition; nightmare_present={}; nightmare_completed={}; daily_activity_points={}'.format(\n" +
                "                                self._last_nightmare_daily_present, self._last_nightmare_daily_completed, self._last_daily_points))\n";
            string staminaEntry =
                "        if (self._yeyu_selected_operations is not None\n" +
                "                and self.yeyu_operation_enabled('spend-waveplates')):\n" +
                "            self.yeyu_stage('spend-waveplates', 'started',\n" +
                "                            'evaluating current stamina condition; used_stamina={}; daily_reward_ready={}'.format(\n" +
                "                                used_stamina, daily_reward_ready))\n";
            if (normalized.Contains(marker)) {
                if (!normalized.Contains(nightmareEntry + nightmareAnchor) ||
                    !normalized.Contains(staminaEntry + staminaAnchor) ||
                    normalized.Contains(staminaAction))
                    throw new InvalidOperationException("ww_conditional_stage_patch_changed");
                return source;
            }
            foreach (string anchor in new[] { nightmareAnchor, staminaAnchor, nightmareAction, staminaAction }) {
                int first = normalized.IndexOf(anchor, StringComparison.Ordinal);
                if (first < 0 || normalized.IndexOf(anchor, first + anchor.Length, StringComparison.Ordinal) >= 0)
                    throw new InvalidOperationException("ww_conditional_stage_source_changed");
            }
            // The operation begins at the actual condition evaluation, while
            // the daily page is still visible. Preserve the upstream terminal
            // and its condition detail; never synthesize a start from a terminal.
            normalized = normalized.Replace(nightmareAction, "'farm-nightmare-daily-echo', 'progress',\n")
                .Replace(staminaAction, "self.yeyu_stage('spend-waveplates', 'progress', 'running configured stamina route')")
                .Replace(nightmareAnchor, nightmareEntry + nightmareAnchor)
                .Replace(staminaAnchor, staminaEntry + staminaAnchor);
            return normalized;
        }
        private static void EnsureWwConditionalStageBoundaries(Binding binding) {
            string path = Path.Combine(Path.GetDirectoryName(binding.InnerEntry), "src", "task", "DailyTask.py");
            string originalHash = Hash(path);
            string source = File.ReadAllText(path, Encoding.UTF8);
            string patched = PatchWwConditionalStageBoundaries(source);
            if (patched == source) return;
            string backup = path + ".yeyu-before-conditional-stages-" + originalHash + ".bak";
            if (!File.Exists(backup)) File.Copy(path, backup, false);
            if (Hash(path) != originalHash || Hash(backup) != originalHash)
                throw new InvalidOperationException("ww_conditional_stage_source_changed_during_patch");
            string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try {
                File.WriteAllText(temporary, patched, new UTF8Encoding(false));
                if (Hash(path) != originalHash)
                    throw new InvalidOperationException("ww_conditional_stage_source_changed_during_patch");
                File.Replace(temporary, path, null);
            } finally {
                if (File.Exists(temporary)) File.Delete(temporary);
            }
            if (File.ReadAllText(path, Encoding.UTF8) != patched)
                throw new InvalidOperationException("ww_conditional_stage_write_failed");
        }
        private static void EnsureWwGuiStreamGuard(Binding binding) {
            const string marker = "# YEYU_GAMER_GUI_STREAM_GUARD_V1";
            string source = File.ReadAllText(binding.InnerEntry, Encoding.UTF8);
            if (source.Contains(marker)) return;
            if (!source.TrimStart().StartsWith("if __name__ == '__main__':", StringComparison.Ordinal))
                throw new InvalidOperationException("ww_gui_entry_shape_changed");
            string guard = marker + "\n" +
                "import os\n" +
                "import sys\n" +
                "\n" +
                "def _yeyu_guard_gui_stream(name):\n" +
                "    stream = getattr(sys, name, None)\n" +
                "    try:\n" +
                "        if stream is None:\n" +
                "            raise OSError('missing GUI stream')\n" +
                "        stream.write('')\n" +
                "        stream.flush()\n" +
                "    except (OSError, ValueError, AttributeError):\n" +
                "        setattr(sys, name, open(os.devnull, 'w', encoding='utf-8'))\n" +
                "\n" +
                "_yeyu_guard_gui_stream('stdout')\n" +
                "_yeyu_guard_gui_stream('stderr')\n" +
                "\n";
            File.WriteAllText(binding.InnerEntry, guard + source, new UTF8Encoding(false));
            if (!File.ReadAllText(binding.InnerEntry, Encoding.UTF8).Contains(marker))
                throw new InvalidOperationException("ww_gui_stream_guard_write_failed");
        }
        private static void EnsureFormalGuiManagerBridge(Binding binding) {
            const string marker = "# YEYU_GAMER_GUI_RUN_BRIDGE_V2";
            string source = File.ReadAllText(binding.InnerEntry, Encoding.UTF8);
            // Remove the V1 manager fragment whose --exit flag caused the
            // upstream TaskExecutor to terminate both tool and game.
            source = source.Replace(
                "        if '--exit' not in _yeyu_sys.argv:\r\n            _yeyu_sys.argv.append('--exit')\r\n", String.Empty)
                .Replace(
                "        if '--exit' not in _yeyu_sys.argv:\n            _yeyu_sys.argv.append('--exit')\n", String.Empty);
            if (source.Contains(marker)) return;
            string normalized = source.Replace("\r\n", "\n");
            string anchor = normalized.Contains("if __name__ == '__main__':\n")
                ? "if __name__ == '__main__':\n"
                : normalized.Contains("if __name__ == \"__main__\":\n")
                    ? "if __name__ == \"__main__\":\n"
                    : null;
            if (anchor == null) throw new InvalidOperationException("formal_gui_entry_shape_changed");
            string bridge = marker + "\n" +
                "import json as _yeyu_json\n" +
                "import os as _yeyu_os\n" +
                "import sys as _yeyu_sys\n" +
                "import time as _yeyu_time\n" +
                "\n" +
                "def _yeyu_manager_run_requested():\n" +
                "    bridge_path = _yeyu_os.path.join(_yeyu_os.path.dirname(__file__), 'configs', 'YeYuGamerRun.json')\n" +
                "    try:\n" +
                "        with open(bridge_path, 'r', encoding='utf-8') as bridge_file:\n" +
                "            bridge = _yeyu_json.load(bridge_file)\n" +
                "        age = _yeyu_time.time() - float(bridge.get('createdAtUnix', 0))\n" +
                "        return (bridge.get('schemaVersion') == 1 and bridge.get('active') is True\n" +
                "                and -300 <= age <= 3600)\n" +
                "    except Exception:\n" +
                "        return False\n" +
                "\n";
            string guardedAnchor = anchor +
                "    if _yeyu_manager_run_requested():\n" +
                "        if '--task' not in _yeyu_sys.argv and '-t' not in _yeyu_sys.argv:\n" +
                "            _yeyu_sys.argv.extend(['--task', '1'])\n" +
                "        # The Manager bridge closes only the tool after the run.\n" +
                "        # PyAppify's --exit path also kills the bound game client.\n";
            normalized = normalized.Replace(anchor, bridge + guardedAnchor);
            File.WriteAllText(binding.InnerEntry, normalized, new UTF8Encoding(false));
            if (!File.ReadAllText(binding.InnerEntry, Encoding.UTF8).Contains(marker))
                throw new InvalidOperationException("ww_gui_manager_bridge_write_failed");
        }
        private static void SetFormalAutoStart(Binding binding, bool enabled) {
            IDictionary<string, object> state = Json.DeserializeObject(File.ReadAllText(binding.UpdateState, Encoding.UTF8)) as IDictionary<string, object>;
            if (state == null) throw new InvalidOperationException("formal_gui_update_state_invalid");
            state["auto_start"] = enabled;
            string temporary = binding.UpdateState + "." + Guid.NewGuid().ToString("N") + ".tmp";
            File.WriteAllText(temporary, Json.Serialize(state), new UTF8Encoding(false));
            if (File.Exists(binding.UpdateState)) File.Replace(temporary, binding.UpdateState, null);
            else File.Move(temporary, binding.UpdateState);
        }
        private static void EnsureEndfieldManagerBridge(Binding binding) {
            const string marker = "# YEYU_GAMER_ENDFIELD_BRIDGE_V2";
            const string preConfigMarker = "# YEYU_GAMER_ENDFIELD_PRECONFIG_BRIDGE_V1";
            string packaged = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "EndfieldYeYuBridge.py");
            if (!File.Exists(packaged)) throw new InvalidOperationException("endfield_bridge_package_missing");
            string patches = Path.Combine(Path.GetDirectoryName(binding.InnerEntry), "src", "patches");
            if (!Directory.Exists(patches)) throw new InvalidOperationException("endfield_patch_root_missing");
            string target = Path.Combine(patches, "yeyu_gamer_bridge.py");
            File.Copy(packaged, target, true);

            string source = File.ReadAllText(binding.InnerEntry, Encoding.UTF8);
            if (!source.Contains(preConfigMarker)) {
                string normalized = source.Replace("\r\n", "\n");
                const string configAnchor = "from src.config import config";
                if (!normalized.Contains(configAnchor)) throw new InvalidOperationException("endfield_gui_config_anchor_changed");
                string preConfigBridge = preConfigMarker + "\n" +
                    "import json as _yeyu_pre_json\n" +
                    "import os as _yeyu_pre_os\n" +
                    "import sys as _yeyu_pre_sys\n" +
                    "import time as _yeyu_pre_time\n" +
                    "\n" +
                    "def _yeyu_pre_config_run_requested():\n" +
                    "    bridge_path = _yeyu_pre_os.path.join(_yeyu_pre_os.path.dirname(__file__), 'configs', 'YeYuGamerRun.json')\n" +
                    "    try:\n" +
                    "        with open(bridge_path, 'r', encoding='utf-8') as bridge_file:\n" +
                    "            bridge = _yeyu_pre_json.load(bridge_file)\n" +
                    "        age = _yeyu_pre_time.time() - float(bridge.get('createdAtUnix', 0))\n" +
                    "        return (bridge.get('schemaVersion') == 1 and bridge.get('active') is True\n" +
                    "                and -300 <= age <= 3600)\n" +
                    "    except Exception:\n" +
                    "        return False\n" +
                    "\n" +
                    "if _yeyu_pre_config_run_requested():\n" +
                    "    if '--task' not in _yeyu_pre_sys.argv and '-t' not in _yeyu_pre_sys.argv:\n" +
                    "        _yeyu_pre_sys.argv.extend(['--task', '1'])\n" +
                    "\n";
                normalized = normalized.Replace(configAnchor, preConfigBridge + configAnchor);
                File.WriteAllText(binding.InnerEntry, normalized, new UTF8Encoding(false));
                source = normalized;
            }
            if (!source.Contains(marker)) {
                string normalized = source.Replace("\r\n", "\n");
                const string anchor = "    install_startup_patches()\n";
                if (!normalized.Contains(anchor)) throw new InvalidOperationException("endfield_gui_entry_shape_changed");
                string injection = anchor +
                    "    " + marker + "\n" +
                    "    from src.patches.yeyu_gamer_bridge import install_yeyu_gamer_bridge\n" +
                    "    install_yeyu_gamer_bridge()\n";
                normalized = normalized.Replace(anchor, injection);
                File.WriteAllText(binding.InnerEntry, normalized, new UTF8Encoding(false));
            }
            string verifiedMain = File.ReadAllText(binding.InnerEntry, Encoding.UTF8);
            string verifiedBridge = File.ReadAllText(target, Encoding.UTF8);
            if (!verifiedMain.Contains(marker) || !verifiedMain.Contains(preConfigMarker) || !verifiedBridge.Contains("_yeyu_gamer_bridge_v2"))
                throw new InvalidOperationException("endfield_manager_bridge_write_failed");
        }
        private static string FormalRunSidecarPath(Binding binding) {
            return Path.Combine(Path.GetDirectoryName(binding.DailyConfig), "YeYuGamerRun.json");
        }
        private static void WriteFormalRunSidecar(Binding binding, string stageFile, string[] selectedOperations) {
            string path = FormalRunSidecarPath(binding);
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            var document = new Dictionary<string, object> {
                { "schemaVersion", 1 }, { "active", true },
                { "createdAtUnix", (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds },
                { "stageFile", Path.GetFullPath(stageFile) },
                { "selectedOperations", selectedOperations }
            };
            File.WriteAllText(temporary, Json.Serialize(document), new UTF8Encoding(false));
            if (File.Exists(path)) File.Replace(temporary, path, null);
            else File.Move(temporary, path);
        }
        private static void ClearFormalRunSidecars(Binding binding) {
            var paths = new HashSet<string>(StringComparer.OrdinalIgnoreCase) {
                FormalRunSidecarPath(binding),
                Path.Combine(binding.ToolRoot, "working", "configs", "YeYuGamerRun.json"),
                Path.Combine(binding.ToolRoot, "data", "apps", binding.AppName ?? String.Empty,
                    "working", "configs", "YeYuGamerRun.json")
            };
            foreach (string path in paths) {
                try { if (File.Exists(path)) File.Delete(path); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
        private static void RefreshFormalLayout(Binding binding) {
            if (String.IsNullOrEmpty(binding.AppName)) return;
            string appRoot = Path.Combine(binding.ToolRoot, "data", "apps", binding.AppName);
            string updateState = Path.Combine(appRoot, "app.json");
            string innerPython = Path.Combine(appRoot, "python", "pythonw.exe");
            string innerEntry = Path.Combine(appRoot, "working", "main.py");
            string dailyConfig = Path.Combine(appRoot, "working", "configs", "DailyTask.json");
            if (!File.Exists(updateState) || !File.Exists(innerPython) ||
                !File.Exists(innerEntry) || !File.Exists(dailyConfig)) return;
            binding.UpdateState = updateState;
            binding.InnerPython = innerPython;
            binding.InnerEntry = innerEntry;
            binding.DailyConfig = dailyConfig;
        }
        private static bool FormalUpdateReady(string path, out string error) {
            return FormalUpdateReady(ReadFormalUpdateObservation(path), out error);
        }
        private static bool FormalUpdateReady(FormalUpdateObservation state, out string error) {
            error = String.Empty;
            try {
                if (state == null || !state.Exists || !state.ValidJson || !state.Installed ||
                    String.IsNullOrEmpty(state.CurrentVersion) ||
                    !String.Equals(state.CurrentVersion, state.StartingVersion, StringComparison.Ordinal)) return false;
                // PyAppify 1.1.6 rewrites app.json without update_state after a
                // successful update pass.  If the field is present it must be
                // idle; if it is absent, matching installed/starting versions
                // plus the fresh formal log below are the completion proof.
                if (!String.IsNullOrWhiteSpace(state.UpdateState) &&
                    !String.Equals(state.UpdateState, "idle", StringComparison.OrdinalIgnoreCase)) return false;
                if (!String.IsNullOrWhiteSpace(state.UpdateError)) {
                    error = state.UpdateError;
                    return false;
                }
                return true;
            } catch (Exception) { return false; }
        }
        private static bool TryStartGf2FormalApplication(Binding binding, Process formal) {
            if (binding.GameId != "GF2" || formal == null) return false;
            try {
                formal.Refresh();
                IntPtr window = formal.MainWindowHandle;
                if (window == IntPtr.Zero) {
                    foreach (Process candidate in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(binding.Executable))) {
                        try {
                            candidate.Refresh();
                            if (candidate.MainWindowHandle == IntPtr.Zero) continue;
                            string image = candidate.MainModule.FileName;
                            if (!String.Equals(Path.GetFullPath(image), Path.GetFullPath(binding.Executable), StringComparison.OrdinalIgnoreCase)) continue;
                            window = candidate.MainWindowHandle;
                            break;
                        } catch { }
                        finally { candidate.Dispose(); }
                    }
                }
                if (window == IntPtr.Zero) return false;
                AutomationElement root = AutomationElement.FromHandle(window);
                foreach (AutomationElement button in root.FindAll(TreeScope.Descendants, Condition.TrueCondition)) {
                    string name = button.Current.Name == null ? String.Empty : button.Current.Name.Trim();
                    if (name != "启动应用") continue;
                    if (!button.Current.IsEnabled || button.Current.IsOffscreen) return false;
                    int clickCount;
                    lock (Gf2FormalStartClicks) {
                        Gf2FormalStartClicks.TryGetValue(window, out clickCount);
                        if (clickCount >= 2) return true;
                    }
                    object rectangle = button.GetCurrentPropertyValue(AutomationElement.BoundingRectangleProperty);
                    Type rectangleType = rectangle == null ? null : rectangle.GetType();
                    if (rectangleType != null) {
                        double left = Convert.ToDouble(rectangleType.GetProperty("Left").GetValue(rectangle, null));
                        double top = Convert.ToDouble(rectangleType.GetProperty("Top").GetValue(rectangle, null));
                        double width = Convert.ToDouble(rectangleType.GetProperty("Width").GetValue(rectangle, null));
                        double height = Convert.ToDouble(rectangleType.GetProperty("Height").GetValue(rectangle, null));
                        if (width >= 4 && height >= 4) {
                            ShowWindowAsync(window, SwShow);
                            SetForegroundWindow(window);
                            Thread.Sleep(150);
                            if (SetCursorPos((int)Math.Round(left + width / 2), (int)Math.Round(top + height / 2))) {
                                mouse_event(MouseLeftDown, 0, 0, 0, UIntPtr.Zero);
                                Thread.Sleep(60);
                                mouse_event(MouseLeftUp, 0, 0, 0, UIntPtr.Zero);
                                lock (Gf2FormalStartClicks) { Gf2FormalStartClicks[window] = clickCount + 1; }
                                return true;
                            }
                        }
                    }
                    object pattern;
                    if (button.TryGetCurrentPattern(InvokePattern.Pattern, out pattern)) {
                        ((InvokePattern)pattern).Invoke();
                        lock (Gf2FormalStartClicks) { Gf2FormalStartClicks[window] = clickCount + 1; }
                        return true;
                    }
                    return false;
                }
            } catch (ElementNotAvailableException) { }
            catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
            return false;
        }
        private static bool HasFormalLoaded(Binding binding, DateTime notBeforeUtc) {
            try {
                bool loaded = false; bool versionChecked = false; bool latest = false;
                string logRoot = Path.Combine(binding.ToolRoot, "logs");
                if (!Directory.Exists(logRoot)) return false;
                // PyAppify names its rolling file with UTC date on this build;
                // around Beijing midnight DateTime.Now points at the next name.
                // Inspect only freshly-written fixed-root app logs.
                foreach (string log in Directory.GetFiles(logRoot, "app.*")) {
                    if (File.GetLastWriteTimeUtc(log) < notBeforeUtc.AddSeconds(-1)) continue;
                    using (var stream = new FileStream(log, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                    using (var reader = new StreamReader(stream, Encoding.UTF8, true)) {
                        string line;
                        while ((line = reader.ReadLine()) != null) {
                            DateTime loggedAt;
                            if (line.Length < 23 || !DateTime.TryParseExact(line.Substring(0, 23), "yyyy-MM-dd HH:mm:ss.fff",
                                System.Globalization.CultureInfo.InvariantCulture,
                                System.Globalization.DateTimeStyles.AssumeLocal, out loggedAt) ||
                                loggedAt.ToUniversalTime() < notBeforeUtc.AddSeconds(-1)) continue;
                            loaded |= line.Contains("Loaded app '" + binding.AppName + "'");
                            versionChecked |= line.Contains("get_tags_and_current_version done for " + binding.AppName);
                            latest |= (line.Contains("First load, checking update and auto-start conditions") ||
                                       line.Contains("First load, checking for auto-start conditions")) &&
                                      line.Contains("is_latest:true");
                        }
                    }
                }
                return loaded && versionChecked && latest;
            } catch (IOException) { }
            catch (UnauthorizedAccessException) { }
            return false;
        }
        private static void ApplyWwPyAppifyEnvironment(ProcessStartInfo start, Binding binding) {
            IDictionary<string, object> state = Json.DeserializeObject(File.ReadAllText(binding.UpdateState, Encoding.UTF8)) as IDictionary<string, object>;
            if (state == null) throw new InvalidOperationException("formal_gui_update_state_invalid");
            object currentVersion; object startingVersion; object currentProfile;
            if (state.TryGetValue("current_version", out currentVersion) && currentVersion is string)
                start.EnvironmentVariables["PYAPPIFY_APP_VERSION"] = (string)currentVersion;
            if (state.TryGetValue("app_starting_version", out startingVersion) && startingVersion is string)
                start.EnvironmentVariables["PYAPPIFY_APP_STARTING_VERSION"] = (string)startingVersion;
            if (state.TryGetValue("current_profile", out currentProfile) && currentProfile is string)
                start.EnvironmentVariables["PYAPPIFY_APP_PROFILE"] = (string)currentProfile;
            start.EnvironmentVariables["PYAPPIFY_APP_JSON_PATH"] = binding.UpdateState;
            start.EnvironmentVariables["PYAPPIFY_EXECUTABLE"] = binding.Executable;
            start.EnvironmentVariables["PYAPPIFY_UPGRADEABLE"] = "1";
            start.EnvironmentVariables["PYAPPIFY_LOCALE"] = System.Globalization.CultureInfo.CurrentUICulture.Name;
            start.EnvironmentVariables["PYAPPIFY_UPDATE_NOTE"] = String.Empty;
            start.EnvironmentVariables["PYAPPIFY_VERSION"] = FileVersionInfo.GetVersionInfo(binding.Executable).FileVersion ?? String.Empty;
            start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            start.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
            start.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
            start.EnvironmentVariables["PYTHONPATH"] = String.Empty;
        }
        private static Process FindFormalInner(Binding binding, DateTime notBeforeUtc) {
            Process match = null;
            Process[] candidates = Process.GetProcessesByName(Path.GetFileNameWithoutExtension(binding.InnerPython));
            foreach (Process candidate in candidates) {
                try {
                    if (candidate.StartTime.ToUniversalTime() < notBeforeUtc.AddSeconds(-1)) { candidate.Dispose(); continue; }
                    string image = null;
                    try { image = candidate.MainModule.FileName; }
                    catch (System.ComponentModel.Win32Exception) { }
                    catch (InvalidOperationException) { }
                    if (!String.IsNullOrEmpty(image) && !Path.GetFullPath(image).StartsWith(
                        Path.GetFullPath(binding.ToolRoot).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar,
                        StringComparison.OrdinalIgnoreCase)) {
                        candidate.Dispose();
                        continue;
                    }
                    if (match != null) {
                        match.Dispose();
                        candidate.Dispose();
                        return null;
                    }
                    match = candidate;
                } catch (InvalidOperationException) { candidate.Dispose(); }
            }
            return match;
        }
        private static void ConfigureToolOutput(ProcessStartInfo start) {
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            start.StandardOutputEncoding = Encoding.UTF8;
            start.StandardErrorEncoding = Encoding.UTF8;
        }
        private static void DrainToolOutput(Process process) {
            if (process == null) return;
            process.OutputDataReceived += delegate { };
            process.ErrorDataReceived += delegate { };
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
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
                    onStage(operation, state, detail ?? String.Empty);
                    if (state == "capture_before" || state == "capture_after") {
                        File.WriteAllText(
                            stageFile + ".capture.ack",
                            state + "; capturedAt=" + DateTime.UtcNow.ToString("o"),
                            new UTF8Encoding(false));
                    }
                }
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
        private static string DetectFormalBlockingState(Binding binding, Process process) {
            var seen = new HashSet<int>();
            if (process != null) {
                try {
                    seen.Add(process.Id);
                    string primary = DetectFormalBlockingWindow(binding, process);
                    if (!String.IsNullOrEmpty(primary)) return primary;
                } catch (InvalidOperationException) { }
            }
            // A signed PyAppify bootstrap may hand the visible window to a
            // replacement bootstrap or its fixed inner Python process. Sample
            // every fixed executable involved in that handoff; otherwise a
            // visible "continue download" gate can be missed after the first
            // Process object exits and later degrade into a generic timeout.
            foreach (string executable in new string[] { binding.Executable, binding.InnerPython }) {
                if (String.IsNullOrWhiteSpace(executable) || !File.Exists(executable)) continue;
                string expected = Path.GetFullPath(executable);
                foreach (Process candidate in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(expected))) {
                    try {
                        if (!seen.Add(candidate.Id)) continue;
                        string image = candidate.MainModule.FileName;
                        if (String.IsNullOrWhiteSpace(image)) continue;
                        if (!String.Equals(Path.GetFullPath(image), expected, StringComparison.OrdinalIgnoreCase)) continue;
                        string state = DetectFormalBlockingWindow(binding, candidate);
                        if (!String.IsNullOrEmpty(state)) return state;
                    } catch (System.ComponentModel.Win32Exception) { }
                    catch (InvalidOperationException) { }
                    finally { candidate.Dispose(); }
                }
            }
            return String.Empty;
        }
        private static string DetectFormalBlockingWindow(Binding binding, Process process) {
            if (process == null) return String.Empty;
            try {
                process.Refresh();
                if (process.HasExited || process.MainWindowHandle == IntPtr.Zero) return String.Empty;
                AutomationElement root = AutomationElement.FromHandle(process.MainWindowHandle);
                if (root == null) return String.Empty;
                var text = new StringBuilder(root.Current.Name ?? String.Empty);
                AutomationElementCollection elements = root.FindAll(TreeScope.Descendants, Condition.TrueCondition);
                for (int index = 0; index < elements.Count && index < 256 && text.Length < 8192; index++) {
                    if (elements[index].Current.IsOffscreen) continue;
                    string name = elements[index].Current.Name;
                    if (!String.IsNullOrWhiteSpace(name)) text.Append('\n').Append(name);
                }
                string visibleText = text.ToString();
                if (binding.GameId == "Endfield" &&
                    (visibleText.IndexOf("继续下载", StringComparison.OrdinalIgnoreCase) >= 0 ||
                     visibleText.IndexOf("Resume Download", StringComparison.OrdinalIgnoreCase) >= 0))
                    return "launcher_download_required: formal launcher requires the user to continue the download";
                if (binding.GameId == "GF2" &&
                    (visibleText.IndexOf("客户端版本已过时", StringComparison.OrdinalIgnoreCase) >= 0 ||
                     visibleText.IndexOf("版本已过时", StringComparison.OrdinalIgnoreCase) >= 0 ||
                     visibleText.IndexOf("download the latest client", StringComparison.OrdinalIgnoreCase) >= 0))
                    return "client_update_required: the game client requires a foreground update";
            } catch (ElementNotAvailableException) { }
            catch (InvalidOperationException) { }
            return String.Empty;
        }
        private static void ApplyOkWWDailyTaskProfile(Binding binding) {
            IDictionary<string, object> profile = binding.DailyTaskProfile;
            var required = new HashSet<string>(StringComparer.Ordinal) {
                "whichToFarm", "tacetSuppressionNumber", "forgeryChallengeNumber", "materialSelection",
                "farmNightmareNestForDailyEcho"
            };
            if (profile == null || profile.Count != required.Count || profile.Keys.Any(key => !required.Contains(key))) throw new InvalidOperationException("ok_ww_daily_profile_invalid");
            string target = profile["whichToFarm"] as string;
            string material = profile["materialSelection"] as string;
            int tacet = PositiveInt(profile["tacetSuppressionNumber"]);
            int forgery = PositiveInt(profile["forgeryChallengeNumber"]);
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
            // Never inherit a tool preset here: these options contain weekly,
            // inventory-mutating, or client-lifecycle actions and are outside
            // the Manager daily contract.
            config["Additional Tasks to Run After Daily Task"] = new string[0];
            config["Exit After Task"] = false;
            string temporary = binding.DailyConfig + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try {
                File.WriteAllText(temporary, Json.Serialize(config), new UTF8Encoding(false));
                File.Replace(temporary, binding.DailyConfig, null);
            } finally {
                if (File.Exists(temporary)) File.Delete(temporary);
            }
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
