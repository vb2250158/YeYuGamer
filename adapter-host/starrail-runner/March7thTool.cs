using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Automation;

namespace YeYuGamer.StarRailAdapter
{
    internal sealed class ToolRunResult
    {
        public string Outcome;
        public int ExitCode;
        public string LogText;
        public DateTimeOffset StartedAt;
        public DateTimeOffset FinishedAt;
        public string DailyTrainingFramePath;
        public string DailyRewardsFramePath;
    }

    internal sealed class March7thTool
    {
        private const int MaxCapturedTextBytes = 1024 * 1024;
        private static readonly string[] HelperProcessNames =
        {
            "March7th Launcher", "March7th Assistant", "March7th Updater"
        };
        private const string CurrencyWarsEntryFailure = "无法切换到 货币战争-主界面";
        private const string CurrencyWarsSettlementHandoff = "检测到前往结算按钮，尝试点击";
        private const string GameSwitchTimeoutFailure = "尝试启动游戏时发生错误：切换到游戏超时";
        internal const string HomeSceneFailure = "无法切换到 主界面";
        private const string ClickEnterScreen = "当前界面：点击进入";
        private const string CommandPatchFileName = "March7thAssistantBasePatch.b64";
        private const string KnownCommandSha256 = "f7c213ab6bbb89ff2f81886386e51b67ee7158e9c292e38b09ef30297a24fb84";
        private const string PatchedCommandSha256 = "e6910b7a780851e709e940d901209818e766c76c69479e85c9588c73f38718d1";
        private const int CommandPatchOffset = 5804340;
        private const int CommandPatchSlotLength = 6592;
        private const byte VirtualKeyF = 0x46;
        private const uint KeyEventKeyUp = 0x0002;
        private readonly ToolBinding binding;
        private bool formalGuiReady;

        public March7thTool(ToolBinding binding)
        {
            this.binding = binding;
        }

        public string GamePath
        {
            get { return binding.GamePath; }
        }

        public bool TryDetectHumanGate(out string reason)
        {
            return StarRailWindowCapture.TryDetectHumanGate(binding.GamePath, out reason);
        }

        public bool IsBusy()
        {
            foreach (string name in HelperProcessNames)
            {
                Process[] processes;
                try { processes = Process.GetProcessesByName(name); }
                catch { return true; }
                if (processes.Length > 0)
                {
                    foreach (Process process in processes) process.Dispose();
                    return true;
                }
            }
            return false;
        }

        public ToolRunResult Run(string operation, ManualResetEvent cancel, DateTimeOffset leaseExpiresAt)
        {
            CommandBinding command;
            if (!binding.Commands.TryGetValue(operation, out command))
                throw new RunnerValidationException("operation_not_bound", "The operation has no fixed March7th command.");
            return RunFixedTask(command.Task, command.TimeoutSeconds, cancel, leaseExpiresAt);
        }

        public ToolRunResult RunDailyWithSafeRecovery(ManualResetEvent cancel, DateTimeOffset leaseExpiresAt,
            string stagingRoot)
        {
            CommandBinding command;
            if (!binding.Commands.TryGetValue("daily-training-objectives", out command) || command.Task != "daily")
                throw new RunnerValidationException("daily_recovery_binding_invalid", "The fixed March7th daily binding changed.");
            byte[] originalConfig = File.ReadAllBytes(binding.ConfigPath);
            try
            {
                SetTopLevelBoolean(binding.ConfigPath, "daily_memory_one_enable", true);
                ToolRunResult first = RunFixedTask(command.Task, command.TimeoutSeconds, cancel, leaseExpiresAt, stagingRoot);
                if (!NeedsDailyRecovery(first)) return first;
                // Currency Wars is the long-running recovery for an otherwise
                // incomplete daily score.  It must inherit the promoted daily
                // operation timeout; the old 1200-second literal cut a healthy
                // run off at stage 2-5 even though the Manager lease allowed an
                // hour for this exact operation.
                ToolRunResult currencyWars = RunFixedTask("currencywars", command.TimeoutSeconds, cancel, leaseExpiresAt);
                if (IsCurrencyWarsEntryFailure(currencyWars))
                {
                    ToolRunResult tableEntry = EnterCurrencyWarsAtVerifiedTable(cancel);
                    if (tableEntry.Outcome != "clean" || tableEntry.ExitCode != 0)
                        return Combine(first, currencyWars, tableEntry);
                    ToolRunResult currencyWarsRetry = RunFixedTask("currencywars", command.TimeoutSeconds, cancel, leaseExpiresAt);
                    if (currencyWarsRetry.Outcome != "clean" || currencyWarsRetry.ExitCode != 0)
                        return CombineRecovered(first, currencyWars, tableEntry, currencyWarsRetry, null);
                    ToolRunResult secondAfterRetry = RunFixedTask(command.Task, command.TimeoutSeconds, cancel, leaseExpiresAt, stagingRoot);
                    return CombineRecovered(first, currencyWars, tableEntry, currencyWarsRetry, secondAfterRetry);
                }
                if (currencyWars.Outcome != "clean" || currencyWars.ExitCode != 0)
                    return Combine(first, currencyWars, null);
                ToolRunResult second = RunFixedTask(command.Task, command.TimeoutSeconds, cancel, leaseExpiresAt, stagingRoot);
                return Combine(first, currencyWars, second);
            }
            finally
            {
                string temporary = binding.ConfigPath + ".yeyu.tmp";
                File.WriteAllBytes(temporary, originalConfig);
                File.Replace(temporary, binding.ConfigPath, null);
            }
        }

        private ToolRunResult RunFixedTask(string task, int timeoutSeconds, ManualResetEvent cancel,
            DateTimeOffset leaseExpiresAt, string dailyEvidenceRoot = null)
        {
            if (!HasGameProcess())
                throw new RunnerValidationException("game_not_started_by_manager", "The fixed StarRail client is not running; the Adapter will not launch it directly.");
            Dictionary<string, long> baseline = SnapshotLogs();
            DateTimeOffset startedAt = DateTimeOffset.UtcNow;
            int seconds = Math.Min(timeoutSeconds, Math.Max(1, (int)Math.Floor((leaseExpiresAt - startedAt).TotalSeconds) - 2));
            try { EnsureFormalGuiReady(cancel, leaseExpiresAt); }
            catch (RunnerValidationException error)
            {
                if (error.Code != "cancelled") throw;
                return new ToolRunResult
                {
                    Outcome = "cancelled", ExitCode = -1,
                    LogText = error.Message, StartedAt = startedAt,
                    FinishedAt = DateTimeOffset.UtcNow
                };
            }
            byte[] originalConfig = File.ReadAllBytes(binding.ConfigPath);
            byte[] originalCommand = null;
            StringBuilder standard = new StringBuilder();
            standard.AppendLine(FormalUpdateEntryRequested()
                ? "[adapter] Manager-owned StarRail is available and the March7th formal GUI update entry completed."
                : "[adapter] Manager-owned StarRail is available; the March7th in-place update check is disabled for unattended runs (verified local patch, maintenance-only updates).");
            standard.AppendLine("[adapter] The selected task was started through March7th Launcher.exe with its documented visible GUI task entry.");
            Process process = new Process();
            bool timedOut = false;
            bool cancelled = false;
            bool fixedTaskFailure = false;
            bool gameSwitchFailure = false;
            bool observedTaskAssistant = false;
            bool idleFormalLauncher = false;
            bool currencyWarsSettlementCompleted = false;
            string previousFreshLogs = String.Empty;
            DateTimeOffset lastFreshLogChangeAt = DateTimeOffset.UtcNow;
            int titleHandoffCount = 0;
            DateTimeOffset nextTitleHandoffAt = DateTimeOffset.MinValue;
            int helperExitCode = 0;
            string dailyTrainingFramePath = null;
            string dailyRewardsFramePath = null;
            try
            {
                SetTopLevelBoolean(binding.ConfigPath, "update_via_launcher", false);
                SetTopLevelBoolean(binding.ConfigPath, "use_background_screenshot", true);
                if (!FormalUpdateEntryRequested())
                {
                    // The task entry ("daily -e") also runs the GitHub update
                    // check on start.  Keep the unattended run offline-safe; the
                    // original config is restored in the finally block below.
                    // Configs without the key (older tool builds) are left
                    // untouched instead of failing the daily.  ``auto_update`` is
                    // deliberately not touched: the GUI shows a blocking
                    // disclaimer dialog whenever it is false.
                    TrySetTopLevelBoolean(binding.ConfigPath, "check_update", false);
                }
                originalCommand = ApplyForegroundCompatibilityPatch();
                TryActivateVerifiedGameWindow();
                process.StartInfo = new ProcessStartInfo
                {
                    FileName = binding.FormalLauncherPath,
                    Arguments = task + " -e",
                    WorkingDirectory = binding.Root,
                    UseShellExecute = true,
                    CreateNoWindow = false,
                    WindowStyle = ProcessWindowStyle.Normal
                };
                if (!process.Start())
                    throw new RunnerValidationException("tool_start_failed", "March7th did not start.");
                Stopwatch timer = Stopwatch.StartNew();
                DateTimeOffset nextForegroundAssistAt = DateTimeOffset.UtcNow.AddSeconds(1);
                DateTimeOffset nextDisclaimerProbeAt = DateTimeOffset.UtcNow.AddSeconds(3);
                bool disclaimerAcknowledged = false;
                while (true)
                {
                    if (cancel.WaitOne(0))
                    {
                        cancelled = true;
                        StopOwnedHelpers(process, startedAt);
                        break;
                    }
                    bool taskAssistantRunning = false;
                    foreach (Process helper in OwnedHelpersStartedSince(startedAt))
                    {
                        try
                        {
                            if (helper.Id == process.Id) continue;
                            observedTaskAssistant = true;
                            if (!helper.HasExited) taskAssistantRunning = true;
                            else if (helper.ExitCode != 0) helperExitCode = helper.ExitCode;
                        }
                        catch { }
                        finally { helper.Dispose(); }
                    }
                    bool launcherRunning = false;
                    try { launcherRunning = !process.HasExited; } catch { }
                    if (!launcherRunning && !taskAssistantRunning &&
                        (observedTaskAssistant || HasFreshLogs(baseline) || timer.Elapsed >= TimeSpan.FromSeconds(15)))
                        break;
                    if (launcherRunning && !observedTaskAssistant && !HasFreshLogs(baseline))
                    {
                        // The GUI entry may be parked behind its exact "免责声明"
                        // modal; acknowledge it (only that button) and give the
                        // launcher a fresh idle window to start the assistant.
                        if (!disclaimerAcknowledged && DateTimeOffset.UtcNow >= nextDisclaimerProbeAt)
                        {
                            nextDisclaimerProbeAt = DateTimeOffset.UtcNow.AddSeconds(2);
                            if (TryAcknowledgeDisclaimer(startedAt))
                            {
                                disclaimerAcknowledged = true;
                                standard.AppendLine("[adapter] Acknowledged the exact March7th disclaimer dialog so the task entry could continue.");
                                timer.Restart();
                            }
                        }
                    }
                    if (launcherRunning && !observedTaskAssistant && !HasFreshLogs(baseline) &&
                        timer.Elapsed >= TimeSpan.FromSeconds(45))
                    {
                        idleFormalLauncher = true;
                        standard.AppendLine("[adapter] March7th formal launcher stayed idle without starting its task assistant.");
                        StopOwnedHelpers(process, startedAt);
                        break;
                    }
                    string freshLogs = CollectFreshLogs(baseline);
                    if (!String.Equals(previousFreshLogs, freshLogs, StringComparison.Ordinal))
                    {
                        previousFreshLogs = freshLogs;
                        lastFreshLogChangeAt = DateTimeOffset.UtcNow;
                    }
                    if (String.Equals(task, "daily", StringComparison.OrdinalIgnoreCase) && dailyEvidenceRoot != null)
                    {
                        if (dailyTrainingFramePath == null &&
                            (Regex.IsMatch(freshLogs, @"当前累计分数：[5-9][0-9]{2}/500", RegexOptions.CultureInvariant) ||
                             freshLogs.IndexOf("检测到本日活跃度已满提示", StringComparison.Ordinal) >= 0 ||
                             freshLogs.IndexOf("实训分数已达标", StringComparison.Ordinal) >= 0))
                        {
                            string candidate = Path.Combine(dailyEvidenceRoot,
                                "starrail-daily-training-live-" + Guid.NewGuid().ToString("N") + ".png");
                            string captureReason;
                            if (StarRailWindowCapture.TryCapture(candidate, binding.GamePath, out captureReason))
                            {
                                dailyTrainingFramePath = candidate;
                                standard.AppendLine("[adapter] Captured the live StarRail daily-training frame at the activity-full marker.");
                            }
                            else
                                standard.AppendLine("[adapter] Live StarRail daily-training capture failed: " + captureReason);
                        }
                        if (dailyRewardsFramePath == null &&
                            freshLogs.IndexOf("每日实训奖励完成", StringComparison.Ordinal) >= 0)
                        {
                            string candidate = Path.Combine(dailyEvidenceRoot,
                                "starrail-daily-rewards-live-" + Guid.NewGuid().ToString("N") + ".png");
                            string captureReason;
                            if (StarRailWindowCapture.TryCapture(candidate, binding.GamePath, out captureReason))
                            {
                                dailyRewardsFramePath = candidate;
                                standard.AppendLine("[adapter] Captured the live StarRail daily-reward frame at the reward-complete marker.");
                            }
                            else
                                standard.AppendLine("[adapter] Live StarRail daily-reward capture failed: " + captureReason);
                        }
                    }
                    if (freshLogs.IndexOf(GameSwitchTimeoutFailure, StringComparison.Ordinal) >= 0)
                    {
                        gameSwitchFailure = true;
                        standard.AppendLine("[adapter] March7th reported the exact StarRail foreground-switch timeout; stopped this attempt immediately.");
                        StopOwnedHelpers(process, startedAt);
                        break;
                    }
                    if (freshLogs.IndexOf(HomeSceneFailure, StringComparison.Ordinal) >= 0)
                    {
                        gameSwitchFailure = true;
                        standard.AppendLine("[adapter] March7th could not return to the main scene; stopped the owned helper for human handoff without controlling the current game scene.");
                        StopOwnedHelpers(process, startedAt);
                        break;
                    }
                    if (titleHandoffCount < 3 &&
                        freshLogs.IndexOf(ClickEnterScreen, StringComparison.Ordinal) >= 0 &&
                        DateTimeOffset.UtcNow >= nextTitleHandoffAt)
                    {
                        if (TryEnterVerifiedGameWindow())
                        {
                            titleHandoffCount += 1;
                            standard.AppendLine("[adapter] Sent one bounded click-enter handoff to the verified Manager-owned StarRail window after March7th identified that exact screen.");
                        }
                        nextTitleHandoffAt = DateTimeOffset.UtcNow.AddSeconds(5);
                    }
                    if (String.Equals(task, "currencywars", StringComparison.OrdinalIgnoreCase) &&
                        freshLogs.IndexOf(CurrencyWarsEntryFailure, StringComparison.Ordinal) >= 0)
                    {
                        fixedTaskFailure = true;
                        standard.AppendLine("[adapter] March7th reported the fixed Currency Wars entry failure; stopped this attempt for bounded table-entry recovery.");
                        StopOwnedHelpers(process, startedAt);
                        break;
                    }
                    if (String.Equals(task, "currencywars", StringComparison.OrdinalIgnoreCase) &&
                        freshLogs.IndexOf(CurrencyWarsSettlementHandoff, StringComparison.Ordinal) >= 0 &&
                        DateTimeOffset.UtcNow - lastFreshLogChangeAt >= TimeSpan.FromSeconds(90))
                    {
                        currencyWarsSettlementCompleted = true;
                        standard.AppendLine("[adapter] Currency Wars reached its settlement handoff and produced no further task log for 90 seconds; closed the stuck helper so the fixed daily verification pass can continue.");
                        StopOwnedHelpers(process, startedAt);
                        break;
                    }
                    if (taskAssistantRunning && DateTimeOffset.UtcNow >= nextForegroundAssistAt)
                    {
                        TryActivateVerifiedGameWindow();
                        nextForegroundAssistAt = DateTimeOffset.UtcNow.AddSeconds(2);
                    }
                    if (timer.Elapsed.TotalSeconds >= seconds || DateTimeOffset.UtcNow >= leaseExpiresAt.AddSeconds(-1))
                    {
                        timedOut = true;
                        StopOwnedHelpers(process, startedAt);
                        break;
                    }
                    Thread.Sleep(250);
                }
                bool launcherExited = false;
                try
                {
                    launcherExited = process.HasExited;
                    if (!launcherExited)
                    {
                        process.WaitForExit(5000);
                        launcherExited = process.HasExited;
                    }
                }
                catch { launcherExited = false; }
                int exitCode = launcherExited ? process.ExitCode : -1;
                if (exitCode == 0 && helperExitCode != 0) exitCode = helperExitCode;
                if (fixedTaskFailure) exitCode = 74;
                if (gameSwitchFailure) exitCode = 76;
                if (idleFormalLauncher) exitCode = 77;
                if (currencyWarsSettlementCompleted) exitCode = 0;
                if (!launcherExited)
                    standard.AppendLine("[adapter] March7th launcher did not exit after bounded cleanup; returned control to Manager without an unbounded wait.");
                string logs = CollectFreshLogs(baseline);
                if (standard.Length > 0)
                    logs = logs + Environment.NewLine + standard.ToString();
                logs = Sanitize(logs);
                return new ToolRunResult
                {
                    Outcome = cancelled ? "cancelled" : timedOut ? "timeout" : "clean",
                    ExitCode = exitCode,
                    LogText = String.IsNullOrWhiteSpace(logs) ? "No fresh March7th log text was produced." : logs,
                    StartedAt = startedAt,
                    FinishedAt = DateTimeOffset.UtcNow,
                    DailyTrainingFramePath = dailyTrainingFramePath,
                    DailyRewardsFramePath = dailyRewardsFramePath
                };
            }
            finally
            {
                process.Dispose();
                try
                {
                    if (originalCommand != null) RestoreForegroundCompatibilityPatch(originalCommand);
                }
                finally
                {
                    RestoreFileAtomically(binding.ConfigPath, originalConfig);
                }
            }
        }

        private static bool NeedsDailyRecovery(ToolRunResult result)
        {
            if (result == null || result.Outcome != "clean" || result.ExitCode != 0) return false;
            string log = result.LogText ?? String.Empty;
            if (log.IndexOf("当前累计分数：500/500", StringComparison.Ordinal) >= 0 ||
                log.IndexOf("每日实训已完成", StringComparison.Ordinal) >= 0) return false;
            return Regex.IsMatch(log, "当前累计分数：[1-4]00/500", RegexOptions.CultureInvariant);
        }

        private static bool IsCurrencyWarsEntryFailure(ToolRunResult result)
        {
            return result != null && result.Outcome == "clean" && result.ExitCode == 74 &&
                (result.LogText ?? String.Empty).IndexOf(CurrencyWarsEntryFailure, StringComparison.Ordinal) >= 0;
        }

        private ToolRunResult EnterCurrencyWarsAtVerifiedTable(ManualResetEvent cancel)
        {
            DateTimeOffset startedAt = DateTimeOffset.UtcNow;
            if (cancel.WaitOne(0))
                return FixedInteractionResult("cancelled", -1, "[adapter] Currency Wars table entry was cancelled.", startedAt);
            Process[] processes;
            try { processes = Process.GetProcessesByName("StarRail"); }
            catch { processes = new Process[0]; }
            string expected = Path.GetFullPath(binding.GamePath);
            try
            {
                foreach (Process process in processes)
                {
                    try
                    {
                        if (process.HasExited || process.MainWindowHandle == IntPtr.Zero) continue;
                        bool verified = false;
                        try
                        {
                            verified = String.Equals(Path.GetFullPath(process.MainModule.FileName), expected,
                                StringComparison.OrdinalIgnoreCase);
                        }
                        catch
                        {
                            // Manager already validated and launched the fixed client. The
                            // anti-cheat layer may deny MainModule inspection after startup.
                            verified = String.Equals(process.ProcessName, "StarRail", StringComparison.OrdinalIgnoreCase);
                        }
                        if (!verified) continue;
                        IntPtr window = process.MainWindowHandle;
                        ShowWindowAsync(window, 9);
                        SetForegroundWindow(window);
                        Thread.Sleep(350);
                        if (GetForegroundWindow() != window)
                            return FixedInteractionResult("clean", 75,
                                "[adapter] Refused the fixed F key because the verified StarRail window did not own foreground focus.", startedAt);
                        keybd_event(VirtualKeyF, 0, 0, UIntPtr.Zero);
                        keybd_event(VirtualKeyF, 0, KeyEventKeyUp, UIntPtr.Zero);
                        Thread.Sleep(1500);
                        return FixedInteractionResult("clean", 0,
                            "[adapter] Sent one fixed F interaction to the verified Manager-owned StarRail window, then returned control to March7th.", startedAt);
                    }
                    catch { }
                }
            }
            finally
            {
                foreach (Process process in processes) process.Dispose();
            }
            return FixedInteractionResult("clean", 75,
                "[adapter] No verified visible Manager-owned StarRail window was available for the fixed table interaction.", startedAt);
        }

        private static ToolRunResult FixedInteractionResult(string outcome, int exitCode, string logText, DateTimeOffset startedAt)
        {
            return new ToolRunResult
            {
                Outcome = outcome, ExitCode = exitCode, LogText = logText,
                StartedAt = startedAt, FinishedAt = DateTimeOffset.UtcNow
            };
        }

        private static ToolRunResult Combine(ToolRunResult first, ToolRunResult recovery, ToolRunResult second)
        {
            ToolRunResult last = second ?? recovery;
            string outcome = new[] { first, recovery, second }
                .Where(delegate(ToolRunResult item) { return item != null; })
                .Any(delegate(ToolRunResult item) { return item.Outcome == "cancelled"; }) ? "cancelled" :
                new[] { first, recovery, second }.Where(delegate(ToolRunResult item) { return item != null; })
                .Any(delegate(ToolRunResult item) { return item.Outcome == "timeout"; }) ? "timeout" : "clean";
            int exitCode = new[] { first, recovery, second }
                .Where(delegate(ToolRunResult item) { return item != null && item.ExitCode != 0; })
                .Select(delegate(ToolRunResult item) { return item.ExitCode; }).FirstOrDefault();
            return new ToolRunResult
            {
                Outcome = outcome,
                ExitCode = exitCode,
                LogText = String.Join(Environment.NewLine + "[adapter] fixed daily recovery stage" + Environment.NewLine,
                    new[] { first, recovery, second }.Where(delegate(ToolRunResult item) { return item != null; })
                        .Select(delegate(ToolRunResult item) { return item.LogText; }).ToArray()),
                StartedAt = first.StartedAt,
                FinishedAt = last.FinishedAt,
                DailyTrainingFramePath = new[] { second, recovery, first }
                    .Where(delegate(ToolRunResult item) { return item != null && !String.IsNullOrWhiteSpace(item.DailyTrainingFramePath); })
                    .Select(delegate(ToolRunResult item) { return item.DailyTrainingFramePath; }).FirstOrDefault(),
                DailyRewardsFramePath = new[] { second, recovery, first }
                    .Where(delegate(ToolRunResult item) { return item != null && !String.IsNullOrWhiteSpace(item.DailyRewardsFramePath); })
                    .Select(delegate(ToolRunResult item) { return item.DailyRewardsFramePath; }).FirstOrDefault()
            };
        }

        private static ToolRunResult CombineRecovered(ToolRunResult first, ToolRunResult failedRecovery,
            ToolRunResult interaction, ToolRunResult successfulRetry, ToolRunResult second)
        {
            ToolRunResult[] effective = new[] { interaction, successfulRetry, second }
                .Where(delegate(ToolRunResult item) { return item != null; }).ToArray();
            ToolRunResult last = effective.Last();
            string outcome = effective.Any(delegate(ToolRunResult item) { return item.Outcome == "cancelled"; }) ? "cancelled" :
                effective.Any(delegate(ToolRunResult item) { return item.Outcome == "timeout"; }) ? "timeout" : "clean";
            int exitCode = effective.Where(delegate(ToolRunResult item) { return item.ExitCode != 0; })
                .Select(delegate(ToolRunResult item) { return item.ExitCode; }).FirstOrDefault();
            ToolRunResult[] all = new[] { first, failedRecovery, interaction, successfulRetry, second }
                .Where(delegate(ToolRunResult item) { return item != null; }).ToArray();
            return new ToolRunResult
            {
                Outcome = outcome,
                ExitCode = exitCode,
                LogText = String.Join(Environment.NewLine + "[adapter] fixed daily recovery stage" + Environment.NewLine,
                    all.Select(delegate(ToolRunResult item) { return item.LogText; }).ToArray()),
                StartedAt = first.StartedAt,
                FinishedAt = last.FinishedAt,
                DailyTrainingFramePath = all.Reverse()
                    .Where(delegate(ToolRunResult item) { return !String.IsNullOrWhiteSpace(item.DailyTrainingFramePath); })
                    .Select(delegate(ToolRunResult item) { return item.DailyTrainingFramePath; }).FirstOrDefault(),
                DailyRewardsFramePath = all.Reverse()
                    .Where(delegate(ToolRunResult item) { return !String.IsNullOrWhiteSpace(item.DailyRewardsFramePath); })
                    .Select(delegate(ToolRunResult item) { return item.DailyRewardsFramePath; }).FirstOrDefault()
            };
        }

        private static bool FormalUpdateEntryRequested()
        {
            // Opt-in maintenance switch; the daily queue never sets it.
            return String.Equals(
                Environment.GetEnvironmentVariable("YEYU_STARRAIL_FORMAL_UPDATE_ENTRY"),
                "1",
                StringComparison.Ordinal);
        }

        private static bool TrySetTopLevelBoolean(string path, string key, bool value)
        {
            string text = File.ReadAllText(path, Encoding.UTF8);
            Regex pattern = new Regex("(?m)^" + Regex.Escape(key) + @":\s*(?:true|false)\s*(?:#.*)?$", RegexOptions.CultureInvariant);
            if (pattern.Matches(text).Count != 1) return false;
            SetTopLevelBoolean(path, key, value);
            return true;
        }

        private static void SetTopLevelBoolean(string path, string key, bool value)
        {
            string text = File.ReadAllText(path, Encoding.UTF8);
            Regex pattern = new Regex("(?m)^" + Regex.Escape(key) + @":\s*(?:true|false)\s*(?:#.*)?$", RegexOptions.CultureInvariant);
            MatchCollection matches = pattern.Matches(text);
            if (matches.Count != 1)
                throw new RunnerValidationException("daily_recovery_config_invalid", "The fixed March7th recovery setting changed.");
            string updated = pattern.Replace(text, key + ": " + (value ? "true" : "false"), 1);
            string temporary = path + ".yeyu.tmp";
            File.WriteAllText(temporary, updated, new UTF8Encoding(false));
            File.Replace(temporary, path, null);
        }

        private byte[] ApplyForegroundCompatibilityPatch()
        {
#if YEYU_STARRAIL_TEST_BUILD
            if (String.Equals(Environment.GetEnvironmentVariable("YEYU_STARRAIL_TEST_FAKE"), "1", StringComparison.Ordinal))
                return null;
#endif
            string currentHash = StrictJson.Sha256File(binding.CommandPath);
            if (!String.Equals(currentHash, KnownCommandSha256, StringComparison.Ordinal))
                throw new RunnerValidationException("march7th_patch_version_changed", "March7th Assistant changed after the verified compatibility patch was built; rebuild the Adapter after its formal update.");
            string payloadPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, CommandPatchFileName);
            if (!File.Exists(payloadPath) || StrictJson.IsReparse(payloadPath))
                throw new RunnerValidationException("march7th_patch_payload_missing", "The verified March7th compatibility payload is missing.");
            byte[] payload;
            try { payload = Convert.FromBase64String(File.ReadAllText(payloadPath, Encoding.ASCII)); }
            catch (Exception error) { throw new RunnerValidationException("march7th_patch_payload_invalid", "The verified March7th compatibility payload is invalid: " + error.Message); }
            if (payload.Length <= 0 || payload.Length > CommandPatchSlotLength)
                throw new RunnerValidationException("march7th_patch_payload_invalid", "The verified March7th compatibility payload has an invalid length.");
            byte[] original = File.ReadAllBytes(binding.CommandPath);
            if (original.Length < CommandPatchOffset + CommandPatchSlotLength)
                throw new RunnerValidationException("march7th_patch_version_changed", "March7th Assistant no longer contains the verified patch slot.");
            byte[] patched = (byte[])original.Clone();
            Buffer.BlockCopy(payload, 0, patched, CommandPatchOffset, payload.Length);
            Array.Clear(patched, CommandPatchOffset + payload.Length, CommandPatchSlotLength - payload.Length);
            ReplaceFileAtomically(binding.CommandPath, patched);
            if (!String.Equals(StrictJson.Sha256File(binding.CommandPath), PatchedCommandSha256, StringComparison.Ordinal))
            {
                ReplaceFileAtomically(binding.CommandPath, original);
                throw new RunnerValidationException("march7th_patch_verification_failed", "March7th foreground compatibility patch did not match its verified digest.");
            }
            return original;
        }

        private void RestoreForegroundCompatibilityPatch(byte[] original)
        {
            ReplaceFileAtomically(binding.CommandPath, original);
            if (!String.Equals(StrictJson.Sha256File(binding.CommandPath), KnownCommandSha256, StringComparison.Ordinal))
                throw new RunnerValidationException("march7th_patch_restore_failed", "March7th Assistant could not be restored after the fixed task.");
        }

        private static void RestoreFileAtomically(string path, byte[] content)
        {
            ReplaceFileAtomically(path, content);
        }

        private static void ReplaceFileAtomically(string path, byte[] content)
        {
            Exception last = null;
            for (int attempt = 0; attempt < 20; attempt++)
            {
                string temporary = path + ".yeyu." + Guid.NewGuid().ToString("N") + ".tmp";
                try
                {
                    File.WriteAllBytes(temporary, content);
                    File.Replace(temporary, path, null);
                    return;
                }
                catch (Exception error)
                {
                    last = error;
                    try { if (File.Exists(temporary)) File.Delete(temporary); } catch { }
                    Thread.Sleep(250);
                }
            }
            throw new RunnerValidationException("march7th_file_replace_failed", "A fixed March7th file could not be replaced atomically: " + (last == null ? "unknown error" : last.Message));
        }

        private void EnsureFormalGuiReady(ManualResetEvent cancel, DateTimeOffset leaseExpiresAt)
        {
            if (formalGuiReady) return;
            March7thErrorDialog.CloseAll();
            ClosePreexistingFormalGui(cancel, leaseExpiresAt);
            if (!FormalUpdateEntryRequested())
            {
                // Unattended daily runs do not perform March7th's in-place
                // update check.  The tool carries a verified local compatibility
                // patch, so an in-place update would break the promoted Adapter
                // anyway, and the GitHub check through the system proxy has hung
                // or been rate-limited for the whole 15-minute window before.
                // Updates stay a deliberate maintenance action.
                formalGuiReady = true;
                return;
            }
            DateTimeOffset startedAt = DateTimeOffset.UtcNow;
            DateTimeOffset? cleanLauncherExitObservedAt = null;
            DateTimeOffset? updateDiscoveredAt = null;
            DateTimeOffset? lastUpdateInvokeAttemptAt = null;
            bool updateActionInvoked = false;
            Dictionary<string, long> updateBaseline = SnapshotLogs();
            string launcherHashBefore = StrictJson.Sha256File(binding.FormalLauncherPath);
            string commandHashBefore = StrictJson.Sha256File(binding.CommandPath);
            bool recentLatestConfirmation = HasCurrentGameDayLatestConfirmation();
            using (Process launcher = new Process())
            {
                launcher.StartInfo = new ProcessStartInfo
                {
                    FileName = binding.FormalLauncherPath,
                    WorkingDirectory = binding.Root,
                    UseShellExecute = true,
                    CreateNoWindow = false,
                    WindowStyle = ProcessWindowStyle.Normal
                };
                if (!launcher.Start())
                    throw new RunnerValidationException("formal_gui_start_failed", "March7th formal GUI launcher did not start.");
                DateTimeOffset deadline = startedAt.AddMinutes(15);
                DateTimeOffset leaseDeadline = leaseExpiresAt.AddSeconds(-10);
                if (leaseDeadline < deadline) deadline = leaseDeadline;
                while (DateTimeOffset.UtcNow < deadline)
                {
                    if (cancel.WaitOne(0))
                        throw new RunnerValidationException("cancelled", "Manager cancelled while the March7th formal GUI checked updates.");
                    bool updaterBusy = Process.GetProcessesByName("March7th Updater").Any(delegate(Process item) { item.Dispose(); return true; });
                    bool assistantReady = Process.GetProcessesByName("March7th Assistant").Any(delegate(Process item) { item.Dispose(); return true; });
                    bool launcherExitedCleanly = launcher.HasExited && launcher.ExitCode == 0;
                    if (launcherExitedCleanly && !updaterBusy && !assistantReady)
                    {
                        if (!cleanLauncherExitObservedAt.HasValue)
                            cleanLauncherExitObservedAt = DateTimeOffset.UtcNow;
                    }
                    else
                    {
                        cleanLauncherExitObservedAt = null;
                    }
                    bool stableCleanExit = cleanLauncherExitObservedAt.HasValue &&
                        DateTimeOffset.UtcNow >= cleanLauncherExitObservedAt.Value.AddSeconds(3);
                    string freshUpdateLog = CollectFreshLogs(updateBaseline);
                    bool rateLimited = freshUpdateLog.IndexOf("rate limit", StringComparison.OrdinalIgnoreCase) >= 0 ||
                        freshUpdateLog.IndexOf("API 速率限制", StringComparison.Ordinal) >= 0;
                    if (rateLimited)
                        throw new RunnerValidationException("formal_gui_update_rate_limited", "March7th formal GUI reached GitHub's update-check rate limit; retry after the published reset time.");
                    bool latestConfirmed = freshUpdateLog.IndexOf("当前是最新版本", StringComparison.Ordinal) >= 0 ||
                        freshUpdateLog.IndexOf("当前已是最新版本", StringComparison.Ordinal) >= 0 ||
                        freshUpdateLog.IndexOf("确认已是最新版本", StringComparison.Ordinal) >= 0 ||
                        freshUpdateLog.IndexOf("already on the latest", StringComparison.OrdinalIgnoreCase) >= 0;
                    bool discoveredUpdate = freshUpdateLog.IndexOf("发现新版本", StringComparison.Ordinal) >= 0 ||
                        freshUpdateLog.IndexOf("new version found", StringComparison.OrdinalIgnoreCase) >= 0;
                    if (discoveredUpdate && !updateDiscoveredAt.HasValue)
                        updateDiscoveredAt = DateTimeOffset.UtcNow;
                    bool binariesChanged = HasBoundToolChanged(launcherHashBefore, commandHashBefore);
                    bool visibleFormalGui = HasVisibleFormalGui(startedAt);
                    if (discoveredUpdate && visibleFormalGui && !updaterBusy && !binariesChanged &&
                        (!lastUpdateInvokeAttemptAt.HasValue || DateTimeOffset.UtcNow >= lastUpdateInvokeAttemptAt.Value.AddSeconds(2)))
                    {
                        lastUpdateInvokeAttemptAt = DateTimeOffset.UtcNow;
                        if (TryInvokeOpenSourceUpdate(startedAt))
                            updateActionInvoked = true;
                    }
                    if (updateDiscoveredAt.HasValue && !updateActionInvoked &&
                        DateTimeOffset.UtcNow >= updateDiscoveredAt.Value.AddSeconds(90))
                        throw new RunnerValidationException("formal_gui_update_action_unavailable", "March7th reported a new version, but its formal GUI did not expose the open-source update action to YeYu Gamer.");
                    bool updateReady = discoveredUpdate
                        ? binariesChanged && !updaterBusy && (visibleFormalGui || stableCleanExit || assistantReady)
                        : stableCleanExit || assistantReady || (latestConfirmed && visibleFormalGui) ||
                            (recentLatestConfirmation && visibleFormalGui &&
                                DateTimeOffset.UtcNow >= startedAt.AddSeconds(20));
                    if (!updaterBusy && updateReady)
                    {
                        CloseIdleFormalGui(startedAt, cancel, deadline);
                        formalGuiReady = true;
                        return;
                    }
                    Thread.Sleep(250);
                }
            }
            throw new RunnerValidationException("formal_gui_update_timeout", "March7th formal GUI did not reach an update-ready state before the bounded timeout.");
        }

        private bool TryInvokeOpenSourceUpdate(DateTimeOffset startedAt)
        {
            return TryInvokeOwnedFormalButton(startedAt, new string[] { "立即更新", "Update Now" });
        }

        private bool TryAcknowledgeDisclaimer(DateTimeOffset startedAt)
        {
            // March7th's GUI shows an exact "免责声明" modal on some launches
            // (first GUI start after an update or a new period).  Its only safe
            // choice is the audited acknowledgement button; nothing else on the
            // dialog is ever invoked.  The 2026-07-28 Gamer policy allows this
            // exact-text disclaimer confirmation.
            return TryInvokeOwnedFormalButton(startedAt, new string[] { "我已知晓" });
        }

        private bool TryInvokeOwnedFormalButton(DateTimeOffset startedAt, string[] exactNames)
        {
            List<AutomationElement> candidates = new List<AutomationElement>();
            foreach (Process process in OwnedHelpersStartedSince(startedAt))
            {
                try
                {
                    IntPtr handle = process.MainWindowHandle;
                    if (handle == IntPtr.Zero) continue;
                    AutomationElement root = AutomationElement.FromHandle(handle);
                    AutomationElementCollection buttons = root.FindAll(
                        TreeScope.Descendants,
                        new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button));
                    foreach (AutomationElement button in buttons)
                    {
                        string name;
                        try { name = button.Current.Name ?? String.Empty; }
                        catch { continue; }
                        foreach (string expected in exactNames)
                        {
                            if (String.Equals(name.Trim(), expected, StringComparison.OrdinalIgnoreCase))
                            {
                                candidates.Add(button);
                                break;
                            }
                        }
                    }
                }
                catch { }
                finally { process.Dispose(); }
            }
            AutomationElement selected = candidates
                .Where(delegate(AutomationElement element)
                {
                    try { return element.Current.IsEnabled && !element.Current.IsOffscreen; }
                    catch { return false; }
                })
                .OrderBy(delegate(AutomationElement element)
                {
                    try { return element.Current.BoundingRectangle.Top; }
                    catch { return Double.MaxValue; }
                })
                .FirstOrDefault();
            if (selected == null) return false;
            try
            {
                object pattern;
                if (!selected.TryGetCurrentPattern(InvokePattern.Pattern, out pattern)) return false;
                ((InvokePattern)pattern).Invoke();
                return true;
            }
            catch { return false; }
        }

        private void ClosePreexistingFormalGui(ManualResetEvent cancel, DateTimeOffset leaseExpiresAt)
        {
            List<Process> owned = OwnedHelpersAtRoot();
            if (owned.Count == 0) return;
            foreach (Process process in owned)
            {
                try { if (!process.HasExited) process.CloseMainWindow(); } catch { }
            }
            DateTimeOffset deadline = DateTimeOffset.UtcNow.AddSeconds(8);
            DateTimeOffset leaseDeadline = leaseExpiresAt.AddSeconds(-10);
            if (leaseDeadline < deadline) deadline = leaseDeadline;
            while (DateTimeOffset.UtcNow < deadline)
            {
                if (cancel.WaitOne(0))
                {
                    foreach (Process process in owned) process.Dispose();
                    throw new RunnerValidationException("cancelled", "Manager cancelled while a stale March7th GUI was closing.");
                }
                bool anyRunning = false;
                foreach (Process process in owned)
                    try { if (!process.HasExited) anyRunning = true; } catch { }
                if (!anyRunning) break;
                Thread.Sleep(250);
            }
            foreach (Process process in owned)
            {
                try { if (!process.HasExited) process.Kill(); } catch { }
                finally { process.Dispose(); }
            }
        }

        private List<Process> OwnedHelpersAtRoot()
        {
            List<Process> result = new List<Process>();
            string canonicalRoot = Path.GetFullPath(binding.Root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            foreach (string name in HelperProcessNames)
            {
                Process[] processes;
                try { processes = Process.GetProcessesByName(name); }
                catch { continue; }
                foreach (Process process in processes)
                {
                    bool keep = false;
                    try
                    {
                        if (!process.HasExited)
                        {
                            string image = process.MainModule.FileName;
                            keep = Path.GetFullPath(image).StartsWith(canonicalRoot, StringComparison.OrdinalIgnoreCase);
                        }
                    }
                    catch { }
                    if (keep) result.Add(process); else process.Dispose();
                }
            }
            return result;
        }

        private bool HasVisibleFormalGui(DateTimeOffset startedAt)
        {
            foreach (Process process in OwnedHelpersStartedSince(startedAt))
            {
                try
                {
                    if (process.MainWindowHandle != IntPtr.Zero &&
                        process.MainWindowTitle.IndexOf("March7th Assistant", StringComparison.OrdinalIgnoreCase) >= 0)
                        return true;
                }
                catch { }
                finally { process.Dispose(); }
            }
            return false;
        }

        private bool HasBoundToolChanged(string launcherHashBefore, string commandHashBefore)
        {
            try
            {
                return StrictJson.Sha256File(binding.FormalLauncherPath) != launcherHashBefore ||
                    StrictJson.Sha256File(binding.CommandPath) != commandHashBefore;
            }
            catch { return false; }
        }

        private void CloseIdleFormalGui(DateTimeOffset startedAt, ManualResetEvent cancel, DateTimeOffset deadline)
        {
            List<Process> owned = OwnedHelpersStartedSince(startedAt);
            foreach (Process process in owned)
            {
                try
                {
                    if (!process.HasExited) process.CloseMainWindow();
                }
                catch { }
            }
            DateTimeOffset gracefulDeadline = DateTimeOffset.UtcNow.AddSeconds(8);
            if (gracefulDeadline > deadline) gracefulDeadline = deadline;
            while (DateTimeOffset.UtcNow < gracefulDeadline)
            {
                if (cancel.WaitOne(0))
                {
                    foreach (Process process in owned) process.Dispose();
                    throw new RunnerValidationException("cancelled", "Manager cancelled while the idle March7th update GUI was closing.");
                }
                bool anyRunning = false;
                foreach (Process process in owned)
                {
                    try { if (!process.HasExited) anyRunning = true; } catch { }
                }
                if (!anyRunning) break;
                Thread.Sleep(250);
            }
            foreach (Process process in owned)
            {
                try { if (!process.HasExited) process.Kill(); } catch { }
                finally { process.Dispose(); }
            }
        }

        private List<Process> OwnedHelpersStartedSince(DateTimeOffset startedAt)
        {
            List<Process> result = new List<Process>();
            string canonicalRoot = Path.GetFullPath(binding.Root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            foreach (string name in HelperProcessNames)
            {
                Process[] processes;
                try { processes = Process.GetProcessesByName(name); }
                catch { continue; }
                foreach (Process process in processes)
                {
                    bool keep = false;
                    try
                    {
                        if (!process.HasExited && process.StartTime.ToUniversalTime() >= startedAt.UtcDateTime.AddSeconds(-2))
                        {
                            string image = process.MainModule.FileName;
                            keep = Path.GetFullPath(image).StartsWith(canonicalRoot, StringComparison.OrdinalIgnoreCase);
                        }
                    }
                    catch { }
                    if (keep) result.Add(process); else process.Dispose();
                }
            }
            return result;
        }

        private bool HasGameProcess()
        {
            Process[] processes;
            try { processes = Process.GetProcessesByName("StarRail"); }
            catch { return false; }
            bool found = false;
            string expected = Path.GetFullPath(binding.GamePath);
            foreach (Process process in processes)
            {
                try
                {
                    string image = process.MainModule.FileName;
                    if (String.Equals(Path.GetFullPath(image), expected, StringComparison.OrdinalIgnoreCase))
                    {
                        found = true;
                    }
                }
                catch
                {
                    // The real StarRail client can deny MainModule inspection
                    // after its anti-cheat layer is active. Manager already
                    // validated the fixed path and a stable visible window
                    // immediately before starting this Adapter, so a live
                    // same-name process with a real main window is sufficient
                    // when exact image inspection is unavailable.
                    try
                    {
                        if (!process.HasExited && process.MainWindowHandle != IntPtr.Zero)
                            found = true;
                    }
                    catch { }
                }
                finally { process.Dispose(); }
            }
            return found;
        }

        private bool TryActivateVerifiedGameWindow()
        {
            Process[] processes;
            try { processes = Process.GetProcessesByName("StarRail"); }
            catch { return false; }
            string expected = Path.GetFullPath(binding.GamePath);
            try
            {
                foreach (Process process in processes)
                {
                    try
                    {
                        if (process.HasExited || process.MainWindowHandle == IntPtr.Zero) continue;
                        bool verified;
                        try
                        {
                            verified = String.Equals(Path.GetFullPath(process.MainModule.FileName), expected,
                                StringComparison.OrdinalIgnoreCase);
                        }
                        catch
                        {
                            verified = String.Equals(process.ProcessName, "StarRail", StringComparison.OrdinalIgnoreCase);
                        }
                        if (!verified) continue;
                        IntPtr window = process.MainWindowHandle;
                        IntPtr foreground = GetForegroundWindow();
                        uint currentThread = GetCurrentThreadId();
                        uint foregroundThread = foreground == IntPtr.Zero ? 0 : GetWindowThreadProcessId(foreground, IntPtr.Zero);
                        uint targetThread = GetWindowThreadProcessId(window, IntPtr.Zero);
                        AllowSetForegroundWindow(-1);
                        if (foregroundThread != 0 && foregroundThread != currentThread)
                            AttachThreadInput(currentThread, foregroundThread, true);
                        if (targetThread != 0 && targetThread != currentThread && targetThread != foregroundThread)
                            AttachThreadInput(currentThread, targetThread, true);
                        try
                        {
                            ShowWindowAsync(window, 9);
                            BringWindowToTop(window);
                            SetForegroundWindow(window);
                        }
                        finally
                        {
                            if (targetThread != 0 && targetThread != currentThread && targetThread != foregroundThread)
                                AttachThreadInput(currentThread, targetThread, false);
                            if (foregroundThread != 0 && foregroundThread != currentThread)
                                AttachThreadInput(currentThread, foregroundThread, false);
                        }
                        Thread.Sleep(250);
                        return GetForegroundWindow() == window;
                    }
                    catch { }
                }
                return false;
            }
            finally
            {
                foreach (Process process in processes) process.Dispose();
            }
        }

        private static void AppendBounded(StringBuilder builder, string stream, string line, object sync)
        {
            lock (sync)
            {
                if (Encoding.UTF8.GetByteCount(builder.ToString()) >= MaxCapturedTextBytes) return;
                builder.Append('[').Append(stream).Append("] ").AppendLine(line);
            }
        }

        private Dictionary<string, long> SnapshotLogs()
        {
            string root = Path.Combine(binding.Root, "logs");
            Dictionary<string, long> result = new Dictionary<string, long>(StringComparer.OrdinalIgnoreCase);
            if (!Directory.Exists(root)) return result;
            if (StrictJson.IsReparse(root))
                throw new RunnerValidationException("unsafe_log_root", "The March7th log root is a reparse point.");
            foreach (string path in Directory.GetFiles(root, "*.log", SearchOption.TopDirectoryOnly))
            {
                if (StrictJson.IsReparse(path))
                    throw new RunnerValidationException("unsafe_log_file", "A March7th log file is a reparse point.");
                result[path] = new FileInfo(path).Length;
            }
            return result;
        }

        private string CollectFreshLogs(Dictionary<string, long> baseline)
        {
            string root = Path.Combine(binding.Root, "logs");
            if (!Directory.Exists(root)) return String.Empty;
            List<string> files = Directory.GetFiles(root, "*.log", SearchOption.TopDirectoryOnly)
                .OrderBy(delegate(string path) { return File.GetLastWriteTimeUtc(path); })
                .ToList();
            MemoryStream collected = new MemoryStream();
            foreach (string path in files)
            {
                if (StrictJson.IsReparse(path)) continue;
                long before;
                if (!baseline.TryGetValue(path, out before)) before = 0;
                using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    if (stream.Length < before) before = 0;
                    if (stream.Length <= before) continue;
                    stream.Position = before;
                    byte[] buffer = new byte[8192];
                    while (collected.Length < MaxCapturedTextBytes)
                    {
                        int remaining = (int)Math.Min(buffer.Length, MaxCapturedTextBytes - collected.Length);
                        int read = stream.Read(buffer, 0, remaining);
                        if (read <= 0) break;
                        collected.Write(buffer, 0, read);
                    }
                }
            }
            return collected.Length == 0 ? String.Empty : new UTF8Encoding(false, false).GetString(collected.ToArray());
        }

        private bool HasFreshLogs(Dictionary<string, long> baseline)
        {
            string root = Path.Combine(binding.Root, "logs");
            if (!Directory.Exists(root)) return false;
            foreach (string path in Directory.GetFiles(root, "*.log", SearchOption.TopDirectoryOnly))
            {
                long before;
                if (!baseline.TryGetValue(path, out before)) before = 0;
                try { if (new FileInfo(path).Length > before) return true; } catch { }
            }
            return false;
        }

        private bool TryEnterVerifiedGameWindow()
        {
            Process[] processes;
            try { processes = Process.GetProcessesByName("StarRail"); }
            catch { return false; }
            string expected = Path.GetFullPath(binding.GamePath);
            try
            {
                foreach (Process process in processes)
                {
                    try
                    {
                        if (process.HasExited || process.MainWindowHandle == IntPtr.Zero) continue;
                        bool verified;
                        try
                        {
                            verified = String.Equals(Path.GetFullPath(process.MainModule.FileName), expected,
                                StringComparison.OrdinalIgnoreCase);
                        }
                        catch
                        {
                            verified = String.Equals(process.ProcessName, "StarRail", StringComparison.OrdinalIgnoreCase);
                        }
                        if (!verified || !TryActivateVerifiedGameWindow()) continue;
                        IntPtr window = process.MainWindowHandle;
                        if (GetForegroundWindow() != window) continue;
                        Rect rect;
                        if (!GetClientRect(window, out rect)) continue;
                        Point point = new Point
                        {
                            X = Math.Max(1, (rect.Right - rect.Left) / 2),
                            Y = Math.Max(1, (rect.Bottom - rect.Top) / 2)
                        };
                        if (!ClientToScreen(window, ref point)) continue;
                        SetCursorPos(point.X, point.Y);
                        keybd_event(0x0D, 0, 0, UIntPtr.Zero);
                        keybd_event(0x0D, 0, KeyEventKeyUp, UIntPtr.Zero);
                        mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero);
                        mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero);
                        return true;
                    }
                    catch { }
                }
            }
            finally
            {
                foreach (Process process in processes) process.Dispose();
            }
            return false;
        }

        private bool HasCurrentGameDayLatestConfirmation()
        {
            string root = Path.Combine(binding.Root, "logs");
            if (!Directory.Exists(root) || StrictJson.IsReparse(root)) return false;
            TimeZoneInfo china;
            try { china = TimeZoneInfo.FindSystemTimeZoneById("China Standard Time"); }
            catch { china = TimeZoneInfo.Local; }
            DateTimeOffset now = DateTimeOffset.UtcNow;
            DateTime localNow = TimeZoneInfo.ConvertTime(now, china).DateTime;
            DateTime gameDay = localNow.TimeOfDay < TimeSpan.FromHours(4)
                ? localNow.Date.AddDays(-1)
                : localNow.Date;
            DateTime boundaryLocal = DateTime.SpecifyKind(gameDay.AddHours(4), DateTimeKind.Unspecified);
            DateTimeOffset boundary = new DateTimeOffset(boundaryLocal, china.GetUtcOffset(boundaryLocal));
            foreach (string path in Directory.GetFiles(root, "*.log", SearchOption.TopDirectoryOnly)
                .OrderByDescending(delegate(string item) { return File.GetLastWriteTimeUtc(item); }))
            {
                try
                {
                    if (StrictJson.IsReparse(path) || File.GetLastWriteTimeUtc(path) < boundary.UtcDateTime) continue;
                    byte[] bytes;
                    using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                        FileShare.ReadWrite | FileShare.Delete))
                    {
                        long offset = Math.Max(0, stream.Length - 262144);
                        stream.Position = offset;
                        bytes = new byte[(int)(stream.Length - offset)];
                        int read = 0;
                        while (read < bytes.Length)
                        {
                            int count = stream.Read(bytes, read, bytes.Length - read);
                            if (count <= 0) break;
                            read += count;
                        }
                        if (read != bytes.Length) Array.Resize(ref bytes, read);
                    }
                    string text = new UTF8Encoding(false, false).GetString(bytes);
                    foreach (string line in text.Split(new string[] { "\r\n", "\n" }, StringSplitOptions.RemoveEmptyEntries))
                    {
                        bool confirmed = line.IndexOf("当前是最新版本", StringComparison.Ordinal) >= 0 ||
                            line.IndexOf("当前已是最新版本", StringComparison.Ordinal) >= 0 ||
                            line.IndexOf("确认已是最新版本", StringComparison.Ordinal) >= 0 ||
                            line.IndexOf("already on the latest", StringComparison.OrdinalIgnoreCase) >= 0;
                        if (!confirmed || line.Length < 23) continue;
                        DateTime observedLocal;
                        if (!DateTime.TryParseExact(line.Substring(0, 23), "yyyy-MM-dd HH:mm:ss,fff",
                            CultureInfo.InvariantCulture, DateTimeStyles.None, out observedLocal)) continue;
                        observedLocal = DateTime.SpecifyKind(observedLocal, DateTimeKind.Unspecified);
                        DateTimeOffset observed = new DateTimeOffset(observedLocal, china.GetUtcOffset(observedLocal));
                        if (observed >= boundary && observed <= now.AddMinutes(5)) return true;
                    }
                }
                catch { }
            }
            return false;
        }

        private void StopOwnedHelpers(Process direct, DateTimeOffset startedAt)
        {
            try { if (!direct.HasExited) direct.Kill(); } catch { }
            string canonicalRoot = Path.GetFullPath(binding.Root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            foreach (string name in HelperProcessNames)
            {
                Process[] processes;
                try { processes = Process.GetProcessesByName(name); }
                catch { continue; }
                foreach (Process candidate in processes)
                {
                    try
                    {
                        if (candidate.Id == direct.Id) continue;
                        if (candidate.StartTime.ToUniversalTime() < startedAt.UtcDateTime.AddSeconds(-2)) continue;
                        string image = candidate.MainModule.FileName;
                        if (Path.GetFullPath(image).StartsWith(canonicalRoot, StringComparison.OrdinalIgnoreCase))
                            candidate.Kill();
                    }
                    catch { }
                    finally { candidate.Dispose(); }
                }
            }
        }

        [DllImport("user32.dll")]
        private static extern bool ShowWindowAsync(IntPtr window, int command);

        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr window);

        [DllImport("user32.dll")]
        private static extern bool BringWindowToTop(IntPtr window);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr window, IntPtr processId);

        [DllImport("user32.dll")]
        private static extern bool AttachThreadInput(uint attach, uint attachTo, bool value);

        [DllImport("user32.dll")]
        private static extern bool AllowSetForegroundWindow(int processId);

        [DllImport("kernel32.dll")]
        private static extern uint GetCurrentThreadId();

        [DllImport("user32.dll")]
        private static extern IntPtr GetForegroundWindow();

        [DllImport("user32.dll")]
        private static extern void keybd_event(byte virtualKey, byte scanCode, uint flags, UIntPtr extraInfo);

        [StructLayout(LayoutKind.Sequential)]
        private struct Rect
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct Point
        {
            public int X;
            public int Y;
        }

        [DllImport("user32.dll")]
        private static extern bool GetClientRect(IntPtr window, out Rect rect);

        [DllImport("user32.dll")]
        private static extern bool ClientToScreen(IntPtr window, ref Point point);

        [DllImport("user32.dll")]
        private static extern bool SetCursorPos(int x, int y);

        [DllImport("user32.dll")]
        private static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extraInfo);

        private string Sanitize(string text)
        {
            string result = text.Replace(binding.Root, "[march7th-root]");
            string profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            if (!String.IsNullOrEmpty(profile)) result = result.Replace(profile, "[user-profile]");
            if (Encoding.UTF8.GetByteCount(result) <= MaxCapturedTextBytes) return result;
            byte[] bytes = Encoding.UTF8.GetBytes(result);
            return new UTF8Encoding(false, false).GetString(bytes, bytes.Length - MaxCapturedTextBytes, MaxCapturedTextBytes);
        }
    }

    internal static class March7thErrorDialog
    {
        private delegate bool EnumWindowsProc(IntPtr window, IntPtr parameter);
        private const uint WmClose = 0x0010;

        [DllImport("user32.dll")]
        private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetWindowText(IntPtr window, StringBuilder text, int maximum);

        [DllImport("user32.dll")]
        private static extern bool IsWindowVisible(IntPtr window);

        [DllImport("user32.dll")]
        private static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);

        public static void CloseAll()
        {
            EnumWindows(delegate(IntPtr window, IntPtr parameter)
            {
                if (!IsWindowVisible(window)) return true;
                StringBuilder title = new StringBuilder(512);
                GetWindowText(window, title, title.Capacity);
                string value = title.ToString();
                if (value.StartsWith("March7th Launcher.exe - Application Error", StringComparison.Ordinal) ||
                    value.StartsWith("March7th Assistant.exe - Application Error", StringComparison.Ordinal) ||
                    value.StartsWith("March7th Updater.exe - Application Error", StringComparison.Ordinal))
                    PostMessage(window, WmClose, IntPtr.Zero, IntPtr.Zero);
                return true;
            }, IntPtr.Zero);
        }
    }

    internal static class StarRailWindowCapture
    {
        private delegate bool EnumWindowsProc(IntPtr window, IntPtr parameter);

        [StructLayout(LayoutKind.Sequential)]
        private struct Rect
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
        }

        [DllImport("user32.dll")]
        private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);

        [DllImport("user32.dll")]
        private static extern bool IsWindowVisible(IntPtr window);

        [DllImport("user32.dll")]
        private static extern bool GetWindowRect(IntPtr window, out Rect rectangle);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);

        [DllImport("user32.dll")]
        private static extern bool PrintWindow(IntPtr window, IntPtr deviceContext, uint flags);

        public static bool TryDetectHumanGate(string expectedGamePath, out string reason)
        {
            Bitmap bitmap;
            string captureReason;
            if (!TryCaptureBitmap(expectedGamePath, out bitmap, out captureReason))
            {
                reason = captureReason;
                return false;
            }
            using (bitmap)
            {
                if (!LooksLikeAccountLogin(bitmap))
                {
                    reason = "No StarRail account-login surface was detected in the stable preflight frame.";
                    return false;
                }
            }
            reason = "The stable StarRail frame shows the account/verification login panel before a usable game scene.";
            return true;
        }

        public static bool TryCapture(string outputPath, string expectedGamePath, out string reason)
        {
            Bitmap bitmap;
            if (!TryCaptureBitmap(expectedGamePath, out bitmap, out reason)) return false;
            using (bitmap) bitmap.Save(outputPath, ImageFormat.Png);
            FileInfo file = new FileInfo(outputPath);
            if (!file.Exists || file.Length <= 0 || file.Length > 20L * 1024 * 1024)
            {
                try { File.Delete(outputPath); } catch { }
                reason = "The captured StarRail frame has an invalid size.";
                return false;
            }
            reason = "ok";
            return true;
        }

        public static bool LooksLikeStableDailyTrainingPanel(string path)
        {
            if (String.IsNullOrWhiteSpace(path) || !File.Exists(path) || StrictJson.IsReparse(path)) return false;
            try
            {
                using (Bitmap bitmap = new Bitmap(path))
                {
                    if (bitmap.Width < 640 || bitmap.Height < 360) return false;
                    double brightnessTotal = 0;
                    int brightSamples = 0;
                    const int columns = 60;
                    const int rows = 40;
                    for (int row = 0; row < rows; row++)
                    {
                        int y = (int)(bitmap.Height * (0.25 + (0.55 * row / (rows - 1))));
                        for (int column = 0; column < columns; column++)
                        {
                            int x = (int)(bitmap.Width * (0.20 + (0.60 * column / (columns - 1))));
                            Color color = bitmap.GetPixel(x, y);
                            double brightness = (color.R + color.G + color.B) / 3.0;
                            brightnessTotal += brightness;
                            if (brightness >= 120) brightSamples += 1;
                        }
                    }
                    int sampleCount = columns * rows;
                    double meanBrightness = brightnessTotal / sampleCount;
                    double brightRatio = (double)brightSamples / sampleCount;
                    return meanBrightness >= 100 && brightRatio >= 0.45;
                }
            }
            catch { return false; }
        }

        private static bool TryCaptureBitmap(string expectedGamePath, out Bitmap bitmap, out string reason)
        {
#if YEYU_STARRAIL_TEST_BUILD
            if (String.Equals(Environment.GetEnvironmentVariable("YEYU_STARRAIL_TEST_FAKE"), "1", StringComparison.Ordinal))
            {
                bitmap = new Bitmap(1280, 720, PixelFormat.Format24bppRgb);
                using (Graphics graphics = Graphics.FromImage(bitmap))
                {
                    graphics.Clear(Color.FromArgb(18, 38, 72));
                    using (Brush panel = new SolidBrush(Color.FromArgb(224, 226, 218)))
                    using (Brush marker = new SolidBrush(Color.FromArgb(235, 190, 76)))
                    {
                        graphics.FillRectangle(panel, 120, 90, 1040, 540);
                        graphics.FillEllipse(marker, 560, 270, 160, 160);
                    }
                }
                reason = "deterministic non-uniform StarRail test frame";
                return true;
            }
#endif
            bitmap = null;
            string expected = Path.GetFullPath(expectedGamePath);
            IntPtr selected = IntPtr.Zero;
            Rect selectedRect = new Rect();
            EnumWindows(delegate(IntPtr window, IntPtr parameter)
            {
                if (!IsWindowVisible(window)) return true;
                uint pid;
                GetWindowThreadProcessId(window, out pid);
                if (pid == 0) return true;
                try
                {
                    using (Process process = Process.GetProcessById((int)pid))
                    {
                        if (!String.Equals(process.ProcessName, "StarRail", StringComparison.Ordinal)) return true;
                        try
                        {
                            if (!String.Equals(Path.GetFullPath(process.MainModule.FileName), expected, StringComparison.OrdinalIgnoreCase)) return true;
                        }
                        catch
                        {
                            // The real client can deny MainModule inspection after
                            // anti-cheat becomes active. Manager already launched
                            // the fixed executable and waited for its stable visible
                            // window before this Adapter started, so accept only the
                            // same-name process whose own main window is this handle.
                            if (process.HasExited || process.MainWindowHandle != window) return true;
                        }
                    }
                }
                catch { return true; }
                Rect rectangle;
                if (!GetWindowRect(window, out rectangle)) return true;
                if (rectangle.Right - rectangle.Left < 640 || rectangle.Bottom - rectangle.Top < 360) return true;
                selected = window;
                selectedRect = rectangle;
                return false;
            }, IntPtr.Zero);
            if (selected == IntPtr.Zero)
            {
                reason = "No visible StarRail window with a usable rectangle was found.";
                return false;
            }
            int width = selectedRect.Right - selectedRect.Left;
            int height = selectedRect.Bottom - selectedRect.Top;
            Bitmap captured = new Bitmap(width, height, PixelFormat.Format24bppRgb);
            using (Graphics graphics = Graphics.FromImage(captured))
            {
                IntPtr context = graphics.GetHdc();
                bool printed;
                try { printed = PrintWindow(selected, context, 2); }
                finally { graphics.ReleaseHdc(context); }
                if (!printed || IsUniform(captured))
                {
                    captured.Dispose();
                    reason = "PrintWindow did not return a non-uniform StarRail frame.";
                    return false;
                }
            }
            bitmap = captured;
            reason = "ok";
            return true;
        }

        private static bool LooksLikeAccountLogin(Bitmap bitmap)
        {
            // The official PC account gate is a bright centered panel with two
            // bright credential rows and one wide orange action, surrounded by
            // the dark train/starfield background. Requiring all four regions
            // prevents ordinary bright in-game panels from opening a false gate.
            Rectangle panel = FractionalRectangle(bitmap, 0.32, 0.25, 0.68, 0.82);
            Rectangle forms = FractionalRectangle(bitmap, 0.35, 0.37, 0.66, 0.59);
            Rectangle action = FractionalRectangle(bitmap, 0.35, 0.62, 0.66, 0.71);
            Rectangle outer = FractionalRectangle(bitmap, 0.00, 0.05, 0.25, 0.90);
            return PixelRatio(bitmap, panel, IsBright) >= 0.55 &&
                PixelRatio(bitmap, forms, IsBright) >= 0.75 &&
                PixelRatio(bitmap, action, IsLoginOrange) >= 0.35 &&
                AverageBrightness(bitmap, outer) <= 100.0;
        }

        private static Rectangle FractionalRectangle(Bitmap bitmap, double left, double top, double right, double bottom)
        {
            int x = Math.Max(0, Math.Min(bitmap.Width - 1, (int)(bitmap.Width * left)));
            int y = Math.Max(0, Math.Min(bitmap.Height - 1, (int)(bitmap.Height * top)));
            int width = Math.Max(1, Math.Min(bitmap.Width - x, (int)(bitmap.Width * (right - left))));
            int height = Math.Max(1, Math.Min(bitmap.Height - y, (int)(bitmap.Height * (bottom - top))));
            return new Rectangle(x, y, width, height);
        }

        private static double PixelRatio(Bitmap bitmap, Rectangle region, Func<Color, bool> predicate)
        {
            int matched = 0;
            int count = 0;
            for (int y = region.Top; y < region.Bottom; y += 4)
            for (int x = region.Left; x < region.Right; x += 4)
            {
                if (predicate(bitmap.GetPixel(x, y))) matched++;
                count++;
            }
            return count == 0 ? 0.0 : (double)matched / count;
        }

        private static double AverageBrightness(Bitmap bitmap, Rectangle region)
        {
            long total = 0;
            int count = 0;
            for (int y = region.Top; y < region.Bottom; y += 4)
            for (int x = region.Left; x < region.Right; x += 4)
            {
                Color color = bitmap.GetPixel(x, y);
                total += (color.R + color.G + color.B) / 3;
                count++;
            }
            return count == 0 ? 255.0 : (double)total / count;
        }

        private static bool IsBright(Color color)
        {
            return color.R > 210 && color.G > 210 && color.B > 210;
        }

        private static bool IsLoginOrange(Color color)
        {
            return color.R > 220 && color.G > 130 && color.G < 230 && color.B < 180;
        }

        public static bool TryCreateWatermarkedCopy(string sourcePath, string outputPath, out string reason)
        {
            try
            {
                using (Bitmap source = new Bitmap(sourcePath))
                using (Bitmap marked = new Bitmap(source))
                using (Graphics graphics = Graphics.FromImage(marked))
                {
                    graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
                    graphics.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
                    string stamp = "YeYu Gamer | " + DateTime.UtcNow.AddHours(8).ToString("yyyy-MM-dd HH:mm:ss") + " CST (UTC+8)";
                    float fontSize = Math.Max(18f, Math.Min(32f, marked.Width / 55f));
                    Font font;
                    try { font = new Font("Microsoft YaHei UI", fontSize, FontStyle.Bold, GraphicsUnit.Pixel); }
                    catch { font = new Font(FontFamily.GenericSansSerif, fontSize, FontStyle.Bold, GraphicsUnit.Pixel); }
                    using (font)
                    using (Brush foreground = new SolidBrush(Color.White))
                    using (Brush background = new SolidBrush(Color.FromArgb(190, 0, 0, 0)))
                    {
                        SizeF textSize = graphics.MeasureString(stamp, font);
                        float padding = Math.Max(10f, fontSize * 0.55f);
                        RectangleF box = new RectangleF(
                            Math.Max(0f, marked.Width - textSize.Width - padding * 2f),
                            Math.Max(0f, marked.Height - textSize.Height - padding * 2f),
                            Math.Min(marked.Width, textSize.Width + padding * 2f),
                            Math.Min(marked.Height, textSize.Height + padding * 2f));
                        graphics.FillRectangle(background, box);
                        graphics.DrawString(stamp, font, foreground, box.Left + padding, box.Top + padding);
                    }
                    marked.Save(outputPath, ImageFormat.Png);
                }
                FileInfo output = new FileInfo(outputPath);
                if (!output.Exists || output.Length <= 0 || output.Length > 20L * 1024 * 1024)
                    throw new InvalidDataException("watermarked screenshot size is invalid");
                reason = "ok";
                return true;
            }
            catch (Exception error)
            {
                try { if (File.Exists(outputPath)) File.Delete(outputPath); } catch { }
                reason = "StarRail watermark failed: " + error.GetType().Name;
                return false;
            }
        }

        private static bool IsUniform(Bitmap bitmap)
        {
            int min = 255;
            int max = 0;
            for (int y = 1; y <= 10; y++)
            {
                for (int x = 1; x <= 10; x++)
                {
                    Color color = bitmap.GetPixel(x * (bitmap.Width - 1) / 11, y * (bitmap.Height - 1) / 11);
                    int brightness = (color.R + color.G + color.B) / 3;
                    min = Math.Min(min, brightness);
                    max = Math.Max(max, brightness);
                }
            }
            return max - min < 12;
        }
    }
}
