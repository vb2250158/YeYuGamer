using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Web.Script.Serialization;

internal static class NteTransportTests
{
    private static object Invoke(string name, params object[] args)
    {
        MethodInfo method = typeof(YeYuGamer.NteAdapter.Program).GetMethod(name, BindingFlags.NonPublic | BindingFlags.Static);
        try { return method.Invoke(null, args); }
        catch (TargetInvocationException error) { throw error.InnerException; }
    }

    private static bool Probe(Func<bool> action, int timeoutMs, Func<bool> cancelled)
    {
        return (bool)Invoke("RunReadOnlyProbe", action, TimeSpan.FromMilliseconds(timeoutMs), cancelled);
    }

    private static void Require(bool condition, string reason)
    {
        if (!condition) throw new Exception(reason);
    }

    public static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--fake-helper-sleep")
        {
            Thread.Sleep(30000);
            return 0;
        }
        try
        {
            Require(Probe(delegate { return true; }, 500, null), "interactive readiness result lost");
            Require(!Probe(delegate { return false; }, 500, null), "splash became ready");
            bool invoked = false;
            try
            {
                Probe(delegate { invoked = true; return true; }, 500, delegate { return true; });
                throw new Exception("pre-cancel ignored");
            }
            catch (OperationCanceledException) { }
            Require(!invoked, "cancelled probe was started");

            try
            {
                Probe(delegate { throw new InvalidOperationException("provider_failed"); }, 500, null);
                throw new Exception("provider exception lost");
            }
            catch (InvalidOperationException error) { Require(error.Message == "provider_failed", "wrong provider error"); }

            // Deliberately never release these fake providers. Their worker
            // threads must not hold the test process open after a terminal.
            var blocked = new ManualResetEvent(false);
            var elapsed = Stopwatch.StartNew();
            try
            {
                Probe(delegate { blocked.WaitOne(); return true; }, 200, null);
                throw new Exception("blocked provider became ready");
            }
            catch (TimeoutException error)
            {
                Require(error.Message == "telemetry_missing:ok_nte_formal_accessibility_timeout", "timeout diagnosis lost");
            }
            long timeoutMs = elapsed.ElapsedMilliseconds;
            Require(timeoutMs < 1500, "provider escaped its deadline");

            var cancellation = new ManualResetEvent(false);
            var entered = new ManualResetEvent(false);
            var canceller = new Thread(delegate() { entered.WaitOne(); cancellation.Set(); });
            canceller.IsBackground = true;
            canceller.Start();
            elapsed.Restart();
            try
            {
                Probe(delegate { entered.Set(); blocked.WaitOne(); return true; }, 10000,
                    delegate { return cancellation.WaitOne(0); });
                throw new Exception("blocked provider ignored cancellation");
            }
            catch (OperationCanceledException) { }
            // Exercise the real cleanup wait immediately after the stuck probe.
            // A permanently running helper must leave room for run_terminal in
            // AdapterHost's five-second grace, without touching any process.
            Invoke("WaitForFormalExit", new Func<bool>(delegate { return true; }),
                new Func<bool>(delegate { return cancellation.WaitOne(0); }));
            long cancelAndCleanupMs = elapsed.ElapsedMilliseconds;
            Require(cancelAndCleanupMs < 2000, "cancel cleanup consumed Host grace");

            elapsed.Restart();
            Invoke("WaitForFormalExit", new Func<bool>(delegate { return false; }), null);
            Require(elapsed.ElapsedMilliseconds < 200, "exited helper was unnecessarily awaited");

            cancellation.Reset();
            elapsed.Restart();
            Invoke("WaitForFormalExit", new Func<bool>(delegate { return true; }),
                new Func<bool>(delegate { return elapsed.ElapsedMilliseconds >= 100; }));
            long lateCancelMs = elapsed.ElapsedMilliseconds;
            Require(lateCancelMs < 2000, "cancellation during normal cleanup was ignored");

            // Use two harmless console fixtures. Only the exact child held by
            // RunTool may be stopped; an unrelated process must remain alive.
            var start = new ProcessStartInfo(Assembly.GetExecutingAssembly().Location, "--fake-helper-sleep")
            {
                UseShellExecute = false, CreateNoWindow = true
            };
            using (Process helper = Process.Start(start))
            using (Process unrelated = Process.Start(start))
            {
                int helperId = helper.Id;
                try
                {
                    Type bindingType = typeof(YeYuGamer.NteAdapter.Program).GetNestedType("ToolBinding", BindingFlags.NonPublic);
                    object binding = Activator.CreateInstance(bindingType, true);
                    bindingType.GetField("FormalLauncher").SetValue(binding,
                        Path.Combine(Path.GetDirectoryName(start.FileName), "nonexistent-formal-gui.exe"));
                    elapsed.Restart();
                    Invoke("StopFormalProcesses", binding, DateTimeOffset.UtcNow,
                        new Func<bool>(delegate { return true; }), helper);
                    Require(elapsed.ElapsedMilliseconds < 2000, "owned helper cleanup consumed Host grace");
                    using (Process observed = Process.GetProcessById(unrelated.Id))
                        Require(!observed.HasExited, "unrelated process was stopped");
                    // StopFormalProcesses disposes the owned process handle.
                    // A second lookup must find no live child.
                    bool ownedAlive = false;
                    try
                    {
                        using (Process observed = Process.GetProcessById(helperId))
                            ownedAlive = !observed.WaitForExit(1000);
                    }
                    catch (ArgumentException) { }
                    Require(!ownedAlive, "owned inner helper survived cancellation");
                }
                finally
                {
                    try { if (!helper.HasExited) helper.Kill(); } catch { }
                    try { if (!unrelated.HasExited) unrelated.Kill(); } catch { }
                }
            }

            Console.WriteLine(new JavaScriptSerializer().Serialize(new
            {
                passed = true, cases = 9, gameStarted = false, formalGuiStarted = false,
                blockedProviderTimeoutMs = timeoutMs,
                cancelAndCleanupMs = cancelAndCleanupMs,
                cancellationDuringCleanupMs = lateCancelMs
            }));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error);
            return 1;
        }
    }
}
