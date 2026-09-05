using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

namespace YeYuGamer.StarRailAdapter
{
    internal sealed class ArtifactRecord
    {
        public string ArtifactId;
        public string FileName;
        public string Kind;
    }

    internal sealed class TodoOutcome
    {
        public string Status;
        public string ReasonCode;
        public string Reason;
        public bool Retryable;
    }

    internal static class Program
    {
        private const int MaxRequestCharacters = 262144;

        public static int Main(string[] args)
        {
            try
            {
                RunnerArguments parsed = RunnerArguments.Parse(args);
                if (parsed.ProbeBinding) return Probe();
                return Execute(parsed);
            }
            catch (RunnerValidationException error)
            {
                Console.Error.WriteLine(error.Code);
                return 64;
            }
            catch (Exception error)
            {
                Console.Error.WriteLine("runner_error:" + error.GetType().Name);
                return 70;
            }
        }

        private static int Probe()
        {
            string root = PackageRoot();
            PackageIdentity package = PackageIdentity.Load(root);
            ToolBinding binding = ToolBinding.Load(root);
            March7thTool tool = new March7thTool(binding);
            Dictionary<string, object> result = new Dictionary<string, object>(StringComparer.Ordinal)
            {
                { "schemaVersion", 1 },
                { "probe", "binding" },
                { "ok", true },
                { "gameId", Protocol.GameId },
                { "packageVersion", package.Version },
                { "packageDigest", package.Digest },
                { "formalLauncherSha256", StrictJson.Sha256File(binding.FormalLauncherPath) },
                { "commandSha256", StrictJson.Sha256File(binding.CommandPath) },
                { "configSha256", StrictJson.Sha256File(binding.ConfigPath) },
                { "helperBusy", tool.IsBusy() },
                { "processStarted", false }
            };
            Console.Out.WriteLine(new JavaScriptSerializer().Serialize(result));
            return 0;
        }

        private static int Execute(RunnerArguments args)
        {
            string requestText = ReadLineBounded(Console.In, MaxRequestCharacters);
            ExecuteRequest request = ExecuteRequest.Parse(requestText, args);
            string root = PackageRoot();
            PackageIdentity package = PackageIdentity.Load(root);
            EventWriter events = new EventWriter(request);
            events.Emit("hello", new Dictionary<string, object>
            {
                { "packageId", Protocol.PackageId },
                { "packageVersion", package.Version },
                { "packageDigest", package.Digest },
                { "runnerPid", Process.GetCurrentProcess().Id },
                { "acceptedTodoInstanceIds", request.ExecutableTodoIds.ToArray() }
            });

            ManualResetEvent cancel = new ManualResetEvent(false);
            StartCancelReader(request, cancel);
            string stagingRoot;
            try { stagingRoot = ValidateStagingRoot(request); }
            catch (RunnerValidationException error)
            {
                return TerminalizeSetupFailure(request, events, null, error, "review_required");
            }

            using (Mutex controller = new Mutex(false, "Local\\YeYuGamer.StarRail.ControllerLease"))
            {
                bool acquired = false;
                try
                {
                    try { acquired = controller.WaitOne(0); }
                    catch (AbandonedMutexException) { acquired = true; }
                    if (!acquired)
                        return TerminalizeSetupFailure(request, events, stagingRoot,
                            new RunnerValidationException("controller_busy", "Another StarRail controller lease is active."), "blocked");
                    // March7th exposes a single "daily" command for these two
                    // game actions.  Never invoke that composite command when
                    // Manager selected only one of its two Todo rows.
                    if (HasUnpairedDailyTrainingScope(request.Todos))
                        return RefuseUnpairedDailyTrainingScope(request, events, stagingRoot);
                    bool needsMarch7th = request.Todos.Any(delegate(TodoTarget item) { return Protocol.UsesMarch7th(item.Operation); });
                    March7thTool tool = null;
                    if (needsMarch7th)
                    {
                        ToolBinding binding;
                        try { binding = ToolBinding.Load(root); }
                        catch (RunnerValidationException error)
                        {
                            return TerminalizeSetupFailure(request, events, stagingRoot, error, "review_required");
                        }
                        tool = new March7thTool(binding);
                        if (tool.IsBusy())
                            return TerminalizeSetupFailure(request, events, stagingRoot,
                                new RunnerValidationException("tool_busy", "A March7th helper is already active; no process was started."), "blocked");
                    }
                    return RunPlan(request, events, tool, stagingRoot, cancel);
                }
                finally
                {
                    if (acquired) try { controller.ReleaseMutex(); } catch { }
                }
            }
        }

        private static int RunPlan(ExecuteRequest request, EventWriter events, March7thTool tool, string stagingRoot, ManualResetEvent cancel)
        {
            List<string> attempted = new List<string>();
            List<string> completed = new List<string>();
            string runStatus = "completed";
            string transport = "clean";
            ToolRunResult recentDailyResult = null;
            int index = 0;
            while (index < request.Todos.Count)
            {
                TodoTarget todo = request.Todos[index];
                string attemptId = Guid.NewGuid().ToString();
                StartTodo(events, todo, attemptId);
                attempted.Add(todo.TodoInstanceId);
                TodoTarget pairedClaim = null;
                string pairedClaimAttemptId = null;
                if (todo.Operation == "daily-training-objectives" && index + 1 < request.Todos.Count &&
                    request.Todos[index + 1].Operation == "claim-daily-training-rewards")
                {
                    // March7th executes score completion and reward claim in
                    // one fixed command.  Start both logical Todos before that
                    // command so Manager captures genuine pre-command frames.
                    pairedClaim = request.Todos[index + 1];
                    pairedClaimAttemptId = Guid.NewGuid().ToString();
                    StartTodo(events, pairedClaim, pairedClaimAttemptId);
                    attempted.Add(pairedClaim.TodoInstanceId);
                }
                if (!Protocol.UsesMarch7th(todo.Operation))
                {
                    DailyTaskListVerification verification = DailyTaskListVerifier.Inspect(stagingRoot, request);
                    List<ArtifactRecord> verifierArtifacts;
                    bool verifierHasFrame;
                    if (todo.Operation == "verify-daily-task-list" && recentDailyResult != null)
                    {
                        string verifiedDailyFrame = PreferredDailyEvidenceFrame(recentDailyResult);
                        verifierArtifacts = StageLogAndFrame(events, request, todo, attemptId, stagingRoot,
                            recentDailyResult.LogText, tool.GamePath);
                        verifierHasFrame = verifierArtifacts.Any(delegate(ArtifactRecord item) { return item.FileName.EndsWith(".png", StringComparison.Ordinal); });
                        AddLiveDailyFrame(events, todo, attemptId, verifierArtifacts,
                            verifiedDailyFrame, "game-ui-daily-training-panel");
                        AddLiveDailyStepAfter(events, todo, attemptId, verifierArtifacts,
                            verifiedDailyFrame, stagingRoot);
                        if (verification != null)
                            verifierArtifacts.AddRange(StageVerifierEvidence(events, todo, attemptId, stagingRoot, verification));
                    }
                    else
                    {
                        verifierArtifacts = StageVerifierEvidence(events, todo, attemptId, stagingRoot, verification);
                        verifierHasFrame = false;
                    }
                    TodoOutcome verifierOutcome = Evaluate(todo.Operation, recentDailyResult, verifierHasFrame,
                        recentDailyResult != null && !String.IsNullOrWhiteSpace(PreferredDailyEvidenceFrame(recentDailyResult)), verification);
                    FinishTodo(events, todo, attemptId, verifierOutcome, verifierArtifacts);
                    if (verifierOutcome.Status != "completed")
                    {
                        runStatus = "review_required";
                        break;
                    }
                    completed.Add(todo.TodoInstanceId);
                    index += 1;
                    continue;
                }
                if (todo.Operation == "attach-home")
                {
                    string humanGateReason;
                    if (tool.TryDetectHumanGate(out humanGateReason))
                    {
                        List<ArtifactRecord> humanGateArtifacts = StageLogAndFrame(
                            events, request, todo, attemptId, stagingRoot,
                            "[adapter] StarRail preflight stopped before March7th formal GUI launch.\r\n" + humanGateReason,
                            tool.GamePath);
                        FinishTodo(events, todo, attemptId, Outcome(
                            "human_required", "starrail_login_required",
                            humanGateReason + " The game client is preserved; finish login in the visible game window, then resume only through YeYu Gamer.",
                            false), humanGateArtifacts);
                        runStatus = "human_required";
                        transport = "clean";
                        break;
                    }
                }
                ToolRunResult toolResult;
                try
                {
                    toolResult = todo.Operation == "daily-training-objectives"
                        ? tool.RunDailyWithSafeRecovery(cancel, request.ExpiresAt, stagingRoot)
                        : tool.Run(todo.Operation, cancel, request.ExpiresAt);
                }
                catch (RunnerValidationException error)
                {
                    List<ArtifactRecord> diagnostic = StageDiagnostic(events, request, todo, attemptId, stagingRoot, error.Code + ": " + error.Message);
                    bool cancelled = error.Code == "cancelled";
                    FinishTodo(events, todo, attemptId, new TodoOutcome
                    {
                        Status = error.Code == "tool_busy" ? "blocked" : "review_required",
                        ReasonCode = cancelled ? "cancelled_by_manager" : error.Code,
                        Reason = cancelled
                            ? "Manager cancellation stopped only the bound helper; the game client must be preserved."
                            : error.Message,
                        Retryable = true
                    }, diagnostic);
                    if (pairedClaim != null)
                    {
                        List<ArtifactRecord> claimDiagnostic = StageDiagnostic(events, request, pairedClaim,
                            pairedClaimAttemptId, stagingRoot, error.Code + ": " + error.Message);
                        FinishTodo(events, pairedClaim, pairedClaimAttemptId, Outcome("review_required",
                            "combined_daily_command_failed", "The shared March7th daily command failed before reward-claim evidence was available.", true), claimDiagnostic);
                    }
                    runStatus = cancelled ? "cancelled" : error.Code == "tool_busy" ? "blocked" : "review_required";
                    if (cancelled) transport = "cancelled";
                    break;
                }

                List<ArtifactRecord> artifacts = StageLogAndFrame(events, request, todo, attemptId, stagingRoot, toolResult.LogText, tool.GamePath);
                if (todo.Operation == "daily-training-objectives")
                {
                    AddLiveDailyFrame(events, todo, attemptId, artifacts,
                        toolResult.DailyTrainingFramePath, "game-ui-daily-training-panel");
                    AddLiveDailyStepAfter(events, todo, attemptId, artifacts,
                        toolResult.DailyTrainingFramePath, stagingRoot);
                }
                if (todo.Operation == "daily-training-objectives")
                    recentDailyResult = toolResult;
                DailyTaskListVerification visual = NeedsDailyVisualEvidence(todo.Operation)
                    ? DailyTaskListVerifier.Inspect(stagingRoot, request) : null;
                if (visual != null)
                    artifacts.AddRange(StageVerifierEvidence(events, todo, attemptId, stagingRoot, visual));
                TodoOutcome outcome = Evaluate(todo.Operation, toolResult,
                    artifacts.Any(delegate(ArtifactRecord item) { return item.FileName.EndsWith(".png", StringComparison.Ordinal); }),
                    !String.IsNullOrWhiteSpace(toolResult.DailyTrainingFramePath), visual);
                FinishTodo(events, todo, attemptId, outcome, artifacts);
                if (outcome.Status != "completed")
                {
                    if (pairedClaim != null)
                    {
                        List<ArtifactRecord> claimDiagnostic = StageLogAndFrame(events, request, pairedClaim,
                            pairedClaimAttemptId, stagingRoot, toolResult.LogText, tool.GamePath);
                        FinishTodo(events, pairedClaim, pairedClaimAttemptId, Outcome("review_required",
                            "combined_daily_command_incomplete", "The shared March7th daily command did not reach a reward-claim terminal state.", true), claimDiagnostic);
                    }
                    runStatus = NonCompletedRunStatus(outcome.Status, toolResult.Outcome);
                    transport = toolResult.Outcome;
                    break;
                }
                completed.Add(todo.TodoInstanceId);

                if (pairedClaim != null)
                {
                    TodoTarget claim = pairedClaim;
                    string claimAttemptId = pairedClaimAttemptId;
                    string verifiedDailyFrame = PreferredDailyEvidenceFrame(toolResult);
                    List<ArtifactRecord> claimArtifacts = StageLogAndFrame(events, request, claim, claimAttemptId, stagingRoot, toolResult.LogText, tool.GamePath);
                    AddLiveDailyFrame(events, claim, claimAttemptId, claimArtifacts,
                        verifiedDailyFrame, "game-ui-daily-training-panel");
                    AddLiveDailyStepAfter(events, claim, claimAttemptId, claimArtifacts,
                        verifiedDailyFrame, stagingRoot);
                    List<ArtifactRecord> completionPair = StageRewardCompletionPair(
                        events, claim, claimAttemptId, stagingRoot, tool.GamePath,
                        verifiedDailyFrame);
                    claimArtifacts.AddRange(completionPair);
                    DailyTaskListVerification claimVisual = DailyTaskListVerifier.Inspect(stagingRoot, request);
                    claimArtifacts.AddRange(StageVerifierEvidence(events, claim, claimAttemptId, stagingRoot, claimVisual));
                    TodoOutcome claimOutcome = Evaluate(claim.Operation, toolResult,
                        claimArtifacts.Any(delegate(ArtifactRecord item) { return item.FileName.EndsWith(".png", StringComparison.Ordinal); }),
                        !String.IsNullOrWhiteSpace(verifiedDailyFrame), claimVisual);
                    if (claimOutcome.Status == "completed" && !HasRewardCompletionPair(completionPair))
                        claimOutcome = Outcome("review_required", "completion_screenshot_pair_missing",
                            "The reward claim finished, but YeYu Gamer could not create both the original and Beijing-time-watermarked completion screenshots.", true);
                    FinishTodo(events, claim, claimAttemptId, claimOutcome, claimArtifacts);
                    if (claimOutcome.Status != "completed")
                    {
                        runStatus = NonCompletedRunStatus(claimOutcome.Status, toolResult.Outcome);
                        break;
                    }
                    completed.Add(claim.TodoInstanceId);
                    index += 2;
                    continue;
                }
                index += 1;
            }

            List<string> unresolved = request.ExecutableTodoIds.Where(delegate(string id) { return !completed.Contains(id); }).ToList();
            if (unresolved.Count == 0) runStatus = "completed";
            else if (runStatus == "completed") runStatus = "partial";
            EmitRunTerminal(events, runStatus, transport, attempted, completed, unresolved);
            return 0;
        }

        private static bool HasUnpairedDailyTrainingScope(IEnumerable<TodoTarget> todos)
        {
            bool objectives = todos.Any(delegate(TodoTarget item) { return item.Operation == "daily-training-objectives"; });
            bool rewards = todos.Any(delegate(TodoTarget item) { return item.Operation == "claim-daily-training-rewards"; });
            return objectives != rewards;
        }

        private static string PreferredDailyEvidenceFrame(ToolRunResult result)
        {
            if (result == null) return null;
            // Either upstream marker can race the reward animation. Select the
            // first frame that actually looks like the bright daily-training
            // panel, and refuse both transient black overlays instead of treating
            // a merely non-uniform image as completion evidence.
            if (StarRailWindowCapture.LooksLikeStableDailyTrainingPanel(result.DailyRewardsFramePath))
                return result.DailyRewardsFramePath;
            if (StarRailWindowCapture.LooksLikeStableDailyTrainingPanel(result.DailyTrainingFramePath))
                return result.DailyTrainingFramePath;
            return null;
        }

        private static int RefuseUnpairedDailyTrainingScope(ExecuteRequest request, EventWriter events, string stagingRoot)
        {
            const string code = "daily_training_pair_required";
            const string reason = "March7th runs daily training and its reward claim as one fixed upstream command. Select both Todo rows together, or select neither; no March7th process was started.";
            List<string> attempted = new List<string>();
            foreach (TodoTarget todo in request.Todos)
            {
                string attemptId = Guid.NewGuid().ToString();
                StartTodo(events, todo, attemptId);
                attempted.Add(todo.TodoInstanceId);
                List<ArtifactRecord> diagnostic = StageDiagnostic(events, request, todo, attemptId, stagingRoot, code + ": " + reason);
                FinishTodo(events, todo, attemptId, Outcome("review_required", code, reason, false), diagnostic);
            }
            EmitRunTerminal(events, "review_required", "clean", attempted, new List<string>(), request.ExecutableTodoIds);
            return 0;
        }

        private static bool NeedsDailyVisualEvidence(string operation)
        {
            return operation == "daily-training-objectives" || operation == "claim-daily-training-rewards" ||
                operation == "verify-daily-task-list";
        }

        private static TodoOutcome Evaluate(string operation, ToolRunResult result, bool hasFrame,
            bool hasLiveDailyFrame, DailyTaskListVerification visual)
        {
            if (operation == "verify-daily-task-list")
                return visual != null && visual.Complete
                    ? Outcome("completed", visual.ReasonCode, visual.Reason, false)
                    : result != null && DailyActivityConfirmed(result.LogText) && DailyRewardsConfirmed(result.LogText) && hasLiveDailyFrame
                    ? Outcome("completed", "daily_task_list_tool_frame_confirmed", "Fresh March7th completion markers and a non-uniform daily-task-list frame confirm the selected daily scope.", false)
                    : Outcome("review_required", visual == null ? "daily_visual_evidence_missing" : visual.ReasonCode,
                        visual == null ? "No current-attempt March7th completion markers plus a non-uniform daily-task-list frame were available." : visual.Reason, true);
            if (result == null)
                return Outcome("review_required", "operation_unhandled", "The operation has no evidence evaluator.", false);
            string log = result.LogText ?? String.Empty;
            if (result.Outcome == "cancelled")
                return Outcome("review_required", "cancelled_by_manager", "Manager cancellation stopped only the bound helper; the game client was preserved.", true);
            if (result.Outcome == "timeout")
                return Outcome("blocked", "tool_timeout", "The bound March7th task exceeded its lease and was stopped; the game client was preserved.", true);
            string[] humanGates =
            {
                "请重新登录", "请先登录", "登录已失效", "登录状态失效", "验证码",
                "人机验证", "实名验证", "用户协议", "隐私协议", "需要人工操作"
            };
            if (humanGates.Any(delegate(string marker) { return log.IndexOf(marker, StringComparison.Ordinal) >= 0; }))
                return Outcome("human_required", "human_gate_detected",
                    "Fresh March7th evidence requires login, verification, or terms handling. The game client is preserved and automatic retry is forbidden.", false);
            if (log.IndexOf(March7thTool.HomeSceneFailure, StringComparison.Ordinal) >= 0)
                return HomeSceneUnconfirmed("Fresh March7th evidence reports that its bounded attempts could not return to the main scene.");
            if (result.ExitCode != 0)
                return Outcome("blocked", "tool_exit_nonzero", "March7th exited nonzero; exit status alone is never accepted as completion.", true);
            string[] hardFailures =
            {
                "游戏客户端版本过低", "请前往启动器下载最新客户端", "无法识别当前游戏界面",
                "每日实训未完成", "清体力未完成"
            };
            if (hardFailures.Any(delegate(string marker) { return log.IndexOf(marker, StringComparison.Ordinal) >= 0; }))
                return Outcome("blocked", "tool_reported_failure", "Fresh March7th evidence contains a failure marker.", true);

            if (operation == "attach-home")
            {
                bool main = Regex.IsMatch(log, @"(?:当前界面|切换到)：主界面[\t\r ]*$", RegexOptions.Multiline | RegexOptions.CultureInvariant);
                bool foreground = log.IndexOf("窗口已切换到前台", StringComparison.Ordinal) >= 0;
                return main && foreground && hasFrame
                    ? Outcome("completed", "home_frame_confirmed",
                        "Fresh March7th main-scene and foreground markers plus a non-uniform StarRail frame confirm the attached home scene.", false)
                    : HomeSceneUnconfirmed("Fresh main-scene and foreground markers plus a non-uniform StarRail frame were not all available.");
            }
            if (operation == "spend-trailblaze-power")
            {
                bool route = log.IndexOf("开始刷拟造花萼（金）", StringComparison.Ordinal) >= 0;
                bool finished = log.IndexOf("副本任务完成", StringComparison.Ordinal) >= 0;
                bool noSafePower = log.IndexOf("开拓力 < 10", StringComparison.Ordinal) >= 0;
                return (route && finished) || noSafePower
                    ? Outcome("completed", noSafePower ? "no_safe_power_remaining" : "calyx_gold_completed",
                        noSafePower ? "Fresh evidence confirms that less than 10 normal Trailblaze Power remained." : "Fresh evidence confirms completion of Calyx (Golden).", false)
                    : Outcome("blocked", "power_result_unconfirmed", "Fresh evidence did not prove Calyx (Golden) completion or the no-safe-power condition.", true);
            }
            if (operation == "daily-training-objectives")
            {
                bool confirmed = (visual != null && visual.Activity500Confirmed) ||
                    (DailyActivityConfirmed(log) && hasLiveDailyFrame);
                return confirmed
                    ? Outcome("completed", visual != null && visual.Activity500Confirmed ? "daily_training_500_visual_confirmed" : "daily_training_tool_frame_confirmed",
                        visual != null && visual.Activity500Confirmed ? "Current-attempt PNG/OCR evidence confirms 500/500 daily training activity." : "Fresh March7th completion markers and a non-uniform daily-task-list frame confirm daily training completion.", false)
                    : Outcome("review_required", visual == null ? "daily_visual_evidence_missing" : visual.ReasonCode,
                        visual == null ? "Fresh March7th completion markers plus a non-uniform daily-task-list frame are required for acceptance." : visual.Reason, true);
            }
            if (operation == "claim-daily-training-rewards")
            {
                bool confirmed = (visual != null && visual.Complete) ||
                    (DailyActivityConfirmed(log) && DailyRewardsConfirmed(log) && hasLiveDailyFrame);
                return confirmed
                    ? Outcome("completed", visual != null && visual.Complete ? "daily_rewards_visual_confirmed" : "daily_rewards_tool_frame_confirmed",
                        visual != null && visual.Complete ? "Current-attempt PNG/OCR evidence confirms all five reward tiers as claimed after 500/500 activity." : "Fresh March7th reward-completion markers and a non-uniform daily-task-list frame confirm the claim step.", false)
                    : Outcome("review_required", visual == null ? "daily_visual_evidence_missing" : visual.ReasonCode,
                        visual == null ? "Fresh March7th reward markers plus a non-uniform daily-task-list frame are required for acceptance." : visual.Reason, true);
            }
            return Outcome("review_required", "operation_unhandled", "The operation has no evidence evaluator.", false);
        }

        private static bool DailyActivityConfirmed(string log)
        {
            if (String.IsNullOrEmpty(log)) return false;
            return Regex.IsMatch(log, @"当前累计分数：[5-9][0-9]{2}/500", RegexOptions.CultureInvariant) ||
                log.IndexOf("检测到本日活跃度已满提示", StringComparison.Ordinal) >= 0 ||
                log.IndexOf("每日实训已完成", StringComparison.Ordinal) >= 0;
        }

        private static bool DailyRewardsConfirmed(string log)
        {
            if (String.IsNullOrEmpty(log)) return false;
            return log.IndexOf("每日实训奖励完成", StringComparison.Ordinal) >= 0 ||
                log.IndexOf("未检测到每日实训奖励", StringComparison.Ordinal) >= 0;
        }

        private static TodoOutcome Outcome(string status, string code, string reason, bool retryable)
        {
            return new TodoOutcome { Status = status, ReasonCode = code, Reason = reason, Retryable = retryable };
        }

        private static TodoOutcome HomeSceneUnconfirmed(string reason)
        {
            return Outcome("human_required", "home_scene_unconfirmed",
                reason + " The current game scene is preserved. Return to the normal main world manually, then resume through YeYu Gamer; automatic retry is forbidden.", false);
        }

        private static string NonCompletedRunStatus(string todoStatus, string toolOutcome)
        {
            if (toolOutcome == "cancelled") return "cancelled";
            if (todoStatus == "human_required") return "human_required";
            if (todoStatus == "review_required") return "review_required";
            return "blocked";
        }

        private static void StartTodo(EventWriter events, TodoTarget todo, string attemptId)
        {
            events.Emit("todo_attempt_started", new Dictionary<string, object>
            {
                { "todoInstanceId", todo.TodoInstanceId }, { "todoAttemptId", attemptId },
                { "attemptNo", todo.PriorAttempts + 1 }, { "operation", todo.Operation }
            });
        }

        private static List<ArtifactRecord> StageLogAndFrame(EventWriter events, ExecuteRequest request, TodoTarget todo,
            string attemptId, string stagingRoot, string logText, string expectedGamePath)
        {
            List<ArtifactRecord> artifacts = new List<ArtifactRecord>();
            artifacts.Add(StageText(events, todo, attemptId, stagingRoot, logText));
            string frameName = "starrail-" + Guid.NewGuid().ToString("N") + ".png";
            string framePath = Path.Combine(stagingRoot, frameName);
            string captureReason;
            if (StarRailWindowCapture.TryCapture(framePath, expectedGamePath, out captureReason))
                artifacts.Add(StageExisting(events, todo, attemptId, framePath, frameName,
                    NeedsDailyVisualEvidence(todo.Operation) ? "game-ui-daily-task-list" : "game-ui-main-window", "image/png"));
            else
                try { if (File.Exists(framePath)) File.Delete(framePath); } catch { }
            return artifacts;
        }

        private static List<ArtifactRecord> StageRewardCompletionPair(EventWriter events, TodoTarget todo,
            string attemptId, string stagingRoot, string expectedGamePath, string preferredRawPath)
        {
            List<ArtifactRecord> artifacts = new List<ArtifactRecord>();
            string token = Guid.NewGuid().ToString("N");
            string rawName = "starrail-reward-raw-" + token + ".png";
            string rawPath = Path.Combine(stagingRoot, rawName);
            string markedName = "starrail-reward-watermarked-" + token + ".png";
            string markedPath = Path.Combine(stagingRoot, markedName);
            string reason;
            if (!String.IsNullOrWhiteSpace(preferredRawPath) && File.Exists(preferredRawPath) &&
                !StrictJson.IsReparse(preferredRawPath))
                File.Copy(preferredRawPath, rawPath, false);
            else if (!StarRailWindowCapture.TryCapture(rawPath, expectedGamePath, out reason))
                return artifacts;
            if (!StarRailWindowCapture.TryCreateWatermarkedCopy(rawPath, markedPath, out reason))
            {
                try { File.Delete(rawPath); } catch { }
                try { File.Delete(markedPath); } catch { }
                return artifacts;
            }
            artifacts.Add(StageExisting(events, todo, attemptId, rawPath, rawName,
                "game-ui-daily-reward-raw", "image/png"));
            artifacts.Add(StageExisting(events, todo, attemptId, markedPath, markedName,
                "game-ui-daily-reward-watermarked", "image/png"));
            return artifacts;
        }

        private static void AddLiveDailyFrame(EventWriter events, TodoTarget todo, string attemptId,
            List<ArtifactRecord> artifacts, string framePath, string kind)
        {
            if (String.IsNullOrWhiteSpace(framePath) || !File.Exists(framePath) || StrictJson.IsReparse(framePath))
                return;
            string name = Path.GetFileName(framePath);
            if (artifacts.Any(delegate(ArtifactRecord item) { return item.FileName == name; })) return;
            artifacts.Add(StageExisting(events, todo, attemptId, framePath, name, kind, "image/png"));
        }

        private static void AddLiveDailyStepAfter(EventWriter events, TodoTarget todo, string attemptId,
            List<ArtifactRecord> artifacts, string framePath, string stagingRoot)
        {
            if (String.IsNullOrWhiteSpace(framePath) || !File.Exists(framePath) || StrictJson.IsReparse(framePath))
                return;
            string name = "starrail-step-after-" + Guid.NewGuid().ToString("N") + ".png";
            string markedPath = Path.Combine(stagingRoot, name);
            string reason;
            if (!StarRailWindowCapture.TryCreateWatermarkedCopy(framePath, markedPath, out reason))
                return;
            artifacts.Add(StageExisting(events, todo, attemptId, markedPath, name,
                "game-ui-step-after-watermarked", "image/png"));
        }

        private static bool HasRewardCompletionPair(List<ArtifactRecord> artifacts)
        {
            return artifacts.Any(delegate(ArtifactRecord item) { return item.Kind == "game-ui-daily-reward-raw"; }) &&
                artifacts.Any(delegate(ArtifactRecord item) { return item.Kind == "game-ui-daily-reward-watermarked"; });
        }

        private static List<ArtifactRecord> StageVerifierEvidence(EventWriter events, TodoTarget todo,
            string attemptId, string stagingRoot, DailyTaskListVerification verification)
        {
            List<ArtifactRecord> artifacts = new List<ArtifactRecord>();
            HashSet<string> staged = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (DailyVisualEvidenceFile file in verification.EvidenceFiles)
            {
                if (!staged.Add(file.FileName)) continue;
                artifacts.Add(StageExisting(events, todo, attemptId, file.Path, file.FileName, file.Kind, file.MimeType));
            }
            string report = "product-owned StarRail daily-task verifier" + Environment.NewLine +
                "reasonCode=" + verification.ReasonCode + Environment.NewLine +
                "activity500Confirmed=" + verification.Activity500Confirmed.ToString().ToLowerInvariant() + Environment.NewLine +
                "allRewardTiersClaimed=" + verification.AllRewardTiersClaimed.ToString().ToLowerInvariant() + Environment.NewLine +
                "reason=" + verification.Reason;
            artifacts.Add(StageText(events, todo, attemptId, stagingRoot, report, "adapter-verifier-report"));
            return artifacts;
        }

        private static List<ArtifactRecord> StageDiagnostic(EventWriter events, ExecuteRequest request, TodoTarget todo,
            string attemptId, string stagingRoot, string text)
        {
            List<ArtifactRecord> result = new List<ArtifactRecord>();
            if (stagingRoot != null)
                result.Add(StageText(events, todo, attemptId, stagingRoot, text));
            return result;
        }

        private static ArtifactRecord StageText(EventWriter events, TodoTarget todo, string attemptId, string stagingRoot, string text)
        {
            return StageText(events, todo, attemptId, stagingRoot, text, "tool-log-outcome");
        }

        private static ArtifactRecord StageText(EventWriter events, TodoTarget todo, string attemptId, string stagingRoot,
            string text, string kind)
        {
            string name = "starrail-" + Guid.NewGuid().ToString("N") + ".txt";
            string path = Path.Combine(stagingRoot, name);
            string bounded = String.IsNullOrWhiteSpace(text) ? "No fresh tool evidence was produced." : text;
            byte[] bytes = new UTF8Encoding(false).GetBytes(bounded);
            if (bytes.Length > 1024 * 1024)
                bytes = bytes.Skip(bytes.Length - 1024 * 1024).ToArray();
            WriteImmutableArtifact(path, bytes);
            return StageExisting(events, todo, attemptId, path, name, kind, "text/plain");
        }

        private static void WriteImmutableArtifact(string finalPath, byte[] bytes)
        {
            string temporaryPath = finalPath + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                using (FileStream stream = new FileStream(temporaryPath, FileMode.CreateNew, FileAccess.Write,
                    FileShare.None, 65536, FileOptions.WriteThrough))
                {
                    stream.Write(bytes, 0, bytes.Length);
                    stream.Flush(true);
                }
                File.Move(temporaryPath, finalPath);
            }
            finally
            {
                try { if (File.Exists(temporaryPath)) File.Delete(temporaryPath); } catch { }
            }
        }

        private static ArtifactRecord StageExisting(EventWriter events, TodoTarget todo, string attemptId, string path,
            string name, string kind, string mime)
        {
            FileInfo info = new FileInfo(path);
            if (!info.Exists || info.Length <= 0 || StrictJson.IsReparse(path))
                throw new RunnerValidationException("artifact_stage_failed", "A staged artifact is missing or unsafe.");
            string artifactId = Guid.NewGuid().ToString();
            string capturedAt = EventWriter.Timestamp(DateTimeOffset.UtcNow);
            events.Emit("artifact_staged", new Dictionary<string, object>
            {
                { "todoInstanceId", todo.TodoInstanceId }, { "todoAttemptId", attemptId },
                { "artifactId", artifactId }, { "kind", kind }, { "fileName", name },
                { "mimeType", mime }, { "sizeBytes", info.Length }, { "sha256", StrictJson.Sha256File(path) },
                { "capturedAt", capturedAt }
            });
            return new ArtifactRecord { ArtifactId = artifactId, FileName = name, Kind = kind };
        }

        private static void FinishTodo(EventWriter events, TodoTarget todo, string attemptId, TodoOutcome outcome, List<ArtifactRecord> artifacts)
        {
            events.Emit("todo_terminal", new Dictionary<string, object>
            {
                { "todoInstanceId", todo.TodoInstanceId }, { "todoAttemptId", attemptId },
                { "status", outcome.Status }, { "reasonCode", outcome.ReasonCode }, { "reason", outcome.Reason },
                { "retryable", outcome.Retryable },
                { "evidenceArtifactIds", artifacts.Select(delegate(ArtifactRecord item) { return item.ArtifactId; }).ToArray() }
            });
        }

        private static int TerminalizeSetupFailure(ExecuteRequest request, EventWriter events, string stagingRoot,
            RunnerValidationException error, string status)
        {
            TodoTarget todo = request.Todos[0];
            string attemptId = Guid.NewGuid().ToString();
            StartTodo(events, todo, attemptId);
            List<ArtifactRecord> artifacts = StageDiagnostic(events, request, todo, attemptId, stagingRoot, error.Code + ": " + error.Message);
            FinishTodo(events, todo, attemptId, Outcome(status, error.Code, error.Message, status != "human_required"), artifacts);
            string runStatus = status == "review_required" || status == "human_required" ? status : "blocked";
            EmitRunTerminal(events, runStatus, "clean",
                new List<string> { todo.TodoInstanceId }, new List<string>(), new List<string>(request.ExecutableTodoIds));
            return 0;
        }

        private static void EmitRunTerminal(EventWriter events, string status, string transport,
            List<string> attempted, List<string> completed, List<string> unresolved)
        {
            string digest = events.TerminalDigest(status, attempted, completed, unresolved, 0);
            events.Emit("run_terminal", new Dictionary<string, object>
            {
                { "status", status }, { "transportOutcome", transport },
                { "attemptedTodoInstanceIds", attempted.ToArray() },
                { "completedTodoInstanceIds", completed.ToArray() },
                { "unresolvedTodoInstanceIds", unresolved.ToArray() },
                { "terminalEventDigest", digest }, { "exitCode", 0 }
            });
        }

        private static string ValidateStagingRoot(ExecuteRequest request)
        {
            string configured = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
            if (String.IsNullOrWhiteSpace(configured))
                throw new RunnerValidationException("artifact_staging_missing", "Manager artifact staging is unavailable.");
            string root = Path.GetFullPath(configured);
            if (!Path.IsPathRooted(root) || root.StartsWith("\\\\", StringComparison.Ordinal) ||
                !Directory.Exists(root) || StrictJson.IsReparse(root) ||
                !String.Equals(new DirectoryInfo(root).Name, request.RunAttemptId, StringComparison.Ordinal))
                throw new RunnerValidationException("unsafe_artifact_staging", "Manager artifact staging failed containment checks.");
            return root;
        }

        private static void StartCancelReader(ExecuteRequest request, ManualResetEvent cancel)
        {
            Thread thread = new Thread(delegate()
            {
                string line;
                try { line = ReadLineBounded(Console.In, 65536); }
                catch { return; }
                if (String.IsNullOrEmpty(line)) return;
                try
                {
                    IDictionary<string, object> value = StrictJson.Object(line, "cancel control", 65536);
                    StrictJson.ExactKeys(value, new string[]
                    {
                        "schemaVersion", "protocolVersion", "controlType", "runId", "runAttemptId",
                        "fencingToken", "at", "reasonCode"
                    }, "cancel control");
                    if (StrictJson.Integer(value["schemaVersion"], "schemaVersion", 1, 1) == 1 &&
                        StrictJson.Text(value["protocolVersion"], "protocolVersion", 8) == Protocol.ProtocolVersion &&
                        StrictJson.Text(value["controlType"], "controlType", 16) == "cancel" &&
                        StrictJson.Text(value["runId"], "runId", 36) == request.RunId &&
                        StrictJson.Text(value["runAttemptId"], "runAttemptId", 36) == request.RunAttemptId &&
                        StrictJson.Text(value["fencingToken"], "fencingToken", 256) == request.FencingToken)
                    {
                        StrictJson.Time(value["at"], "at");
                        StrictJson.Identifier(value["reasonCode"], "reasonCode");
                        cancel.Set();
                    }
                }
                catch { }
            });
            thread.IsBackground = true;
            thread.Name = "YeYuGamerStarRailCancel";
            thread.Start();
        }

        private static string ReadLineBounded(TextReader reader, int maximum)
        {
            StringBuilder value = new StringBuilder();
            while (true)
            {
                int next = reader.Read();
                if (next < 0)
                {
                    if (value.Length == 0) throw new RunnerValidationException("missing_input", "A bounded JSON line is required.");
                    break;
                }
                if (next == '\n') break;
                if (next == '\r') continue;
                value.Append((char)next);
                if (value.Length > maximum)
                    throw new RunnerValidationException("input_too_large", "The input line exceeded its limit.");
            }
            return value.ToString();
        }

        private static string PackageRoot()
        {
            string root = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory).TrimEnd(Path.DirectorySeparatorChar);
            if (root.StartsWith("\\\\", StringComparison.Ordinal) || !Directory.Exists(root) || StrictJson.IsReparse(root))
                throw new RunnerValidationException("unsafe_package_root", "The runner package root must be a local non-reparse directory.");
            return root;
        }
    }
}
