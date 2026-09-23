using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;

// Replay of the built NTE runner's launcher-retry watchdog.
//
// Ground truth comes from this machine's own attempt logs: two successful
// attempts stopped at two official launcher starts with two positive button
// matches, while every starving attempt reached 24 or more starts, zero
// matches and 146+ "no frame for 10 sec" records. The watchdog must only ever
// end an attempt while the official launcher has made no progress at all, and
// it must stay silent for the official update-wait branch.
internal static class NteLauncherRetryStormReplay
{
    private static Type stormType;
    private static MethodInfo observe;
    private static MethodInfo observeStage;
    private static MethodInfo shouldAbort;
    private static MethodInfo shouldAbortAt;
    private static MethodInfo describe;

    static int Main(string[] args)
    {
        if (args.Length != 1) throw new Exception("usage: NteLauncherRetryStormReplay <runner.exe>");
        stormType = Assembly.LoadFrom(args[0]).GetType("YeYuGamer.NteAdapter.Program+LauncherRetryStorm", true);
        observe = stormType.GetMethod("ObserveLog");
        observeStage = stormType.GetMethod("ObserveStage");
        shouldAbort = stormType.GetMethod("ShouldAbort");
        shouldAbortAt = stormType.GetMethod("ShouldAbortAt");
        describe = stormType.GetMethod("Describe");
        if (observe == null || observeStage == null || shouldAbort == null || shouldAbortAt == null || describe == null)
            throw new Exception("The launcher retry watchdog is missing from the built runner");

        int successFirst = NeverAborts(SuccessStream(), "successful attempt");
        int updateFirst = NeverAborts(UpdateWaitStream(), "official update wait");
        int starvingFirst = AbortIndex(StarvingStream(27, 10, 5), "starving attempt");
        int stormFirst = AbortIndex(StarvingStream(24, 0, 0), "restart storm without frame starvation");
        int frameFirst = AbortIndex(StarvingStream(3, 0, 40), "complete frame starvation with few restarts");

        int starvingTotal = RecordCount(StarvingStream(27, 10, 5));
        int stormTotal = RecordCount(StarvingStream(24, 0, 0));
        if (starvingFirst > (int)(starvingTotal * 0.3))
            throw new Exception("A starving attempt must end early instead of burning the lease: " + starvingFirst + "/" + starvingTotal);
        if (stormFirst > (int)(stormTotal * 0.3))
            throw new Exception("A restart storm must end early instead of burning the lease: " + stormFirst + "/" + stormTotal);

        Boundary();
        ButtonScore();
        LogSilence();
        Console.WriteLine("{\"suite\":\"nte-launcher-retry-storm\",\"passed\":true,\"gameStarted\":false" +
            ",\"successAbortIndex\":" + successFirst + ",\"updateWaitAbortIndex\":" + updateFirst +
            ",\"starvingAbortIndex\":" + starvingFirst + ",\"starvingRecords\":" + starvingTotal +
            ",\"stormAbortIndex\":" + stormFirst + ",\"stormRecords\":" + stormTotal +
            ",\"frameStarvationAbortIndex\":" + frameFirst + "}");
        return 0;
    }

    private static object NewStorm()
    {
        return Activator.CreateInstance(stormType, true);
    }

    private static bool Feed(object storm, string message)
    {
        // The official update-wait branch reaches the runner as a bridge stage
        // observation, exactly as the official logger stream reaches it live.
        if (message.StartsWith("upstream:LauncherTask._extend_deadline_for_update", StringComparison.Ordinal))
            observeStage.Invoke(storm, new object[] { message });
        else
            observe.Invoke(storm, new object[] { new Dictionary<string, object> { { "message", message } } });
        return (bool)shouldAbort.Invoke(storm, null);
    }

    private static int NeverAborts(IEnumerable<string> stream, string label)
    {
        object storm = NewStorm();
        int index = 0;
        foreach (string message in stream)
        {
            if (Feed(storm, message))
                throw new Exception("The watchdog ended a run it must keep: " + label + " at record " + index);
            index++;
        }
        return -1;
    }

    private static int AbortIndex(IEnumerable<string> stream, string label)
    {
        object storm = NewStorm();
        int index = 0;
        foreach (string message in stream)
        {
            if (Feed(storm, message)) return index;
            index++;
        }
        throw new Exception("The watchdog never ended a doomed attempt: " + label);
    }

    private static int RecordCount(IEnumerable<string> stream)
    {
        int count = 0;
        foreach (string ignored in stream) count++;
        return count;
    }

    // Threshold boundary: five official restarts stay silent, the sixth ends it.
    private static void Boundary()
    {
        object below = NewStorm();
        for (int start = 0; start < 5; start++)
        {
            if (Feed(below, "LauncherTask:Launcher task started")) throw new Exception("Five restarts must not end a run");
            Feed(below, "LauncherTask:launcher_button color 0.0");
            Feed(below, "windows_graphics:no frame for 10 sec, try to restart");
        }
        if ((bool)shouldAbort.Invoke(below, null)) throw new Exception("Five restarts must not end a run");
        if (!Feed(below, "LauncherTask:Launcher task started")) throw new Exception("The sixth restart must end the run");

        // A single matched launcher button means the official routine still owns
        // the run, no matter how many restarts follow.
        object matched = NewStorm();
        Feed(matched, "LauncherTask:launcher_button color 1.0");
        for (int start = 0; start < 30; start++)
        {
            Feed(matched, "LauncherTask:Launcher task started");
            Feed(matched, "windows_graphics:no frame for 10 sec, try to restart");
            if ((bool)shouldAbort.Invoke(matched, null))
                throw new Exception("A run that already matched the launcher button must not be ended");
        }
    }

    // The button score marks real progress only near 1.0. This machine's starving
    // attempts emitted 0.0 plus sub-0.03 noise that the official logger itself
    // labels "launcher button not found" — none of those may disable the watchdog.
    private static void ButtonScore()
    {
        object noise = NewStorm();
        Feed(noise, "LauncherTask:launcher_button color 0.00125");
        Feed(noise, "LauncherTask:launcher button not found");
        Feed(noise, "LauncherTask:launcher_button color 0.025");
        Feed(noise, "LauncherTask:launcher_button color 0.4");
        for (int start = 0; start < 30; start++)
        {
            Feed(noise, "LauncherTask:Launcher task started");
            Feed(noise, "LauncherTask:launcher_button color 0.0");
        }
        if (!(bool)shouldAbort.Invoke(noise, null))
            throw new Exception("Sub-threshold button scores must not look like official progress");
    }

    // A wedged capture call stops the official logger; ten minutes of silence
    // after a launcher start is a hang, not a wait.
    private static void LogSilence()
    {
        object hung = NewStorm();
        Feed(hung, "LauncherTask:Launcher task started");
        Feed(hung, "LauncherTask:launcher_button color 0.00125");
        DateTime start = DateTime.UtcNow;
        if (!AbortsAt(hung, start.AddSeconds(660)))
            throw new Exception("Silent official logging must end a wedged attempt");

        object fresh = NewStorm();
        Feed(fresh, "LauncherTask:Launcher task started");
        DateTime held = DateTime.UtcNow;
        if (AbortsAt(fresh, held.AddSeconds(30)))
            throw new Exception("Normal official logging cadence must not end an attempt");

        object updating = NewStorm();
        Feed(updating, "LauncherTask:Launcher task started");
        Feed(updating, "upstream:LauncherTask._extend_deadline_for_update:waiting_for_official_update");
        if (AbortsAt(updating, DateTime.UtcNow.AddSeconds(660)))
            throw new Exception("An official update wait must never be ended for silence");

        object early = NewStorm();
        Feed(early, "ok:ok-script init dev, ['main.py', '--task', '2', '--exit']");
        if (AbortsAt(early, DateTime.UtcNow.AddSeconds(660)))
            throw new Exception("Silence before the official launcher starts is not this watchdog's case");
    }

    private static bool AbortsAt(object storm, DateTime nowUtc)
    {
        return (bool)shouldAbortAt.Invoke(storm, new object[] { nowUtc });
    }

    private static IEnumerable<string> SuccessStream()
    {
        yield return "ok:ok-script init dev, ['main.py', '--task', '2', '--exit'], pid=19540";
        yield return "LauncherTask:Launcher task started";
        yield return "windows_graphics:no frame for 10 sec, try to restart";
        yield return "LauncherTask:launcher_button color 1.0";
        yield return "LauncherTask:Launcher task started";
        yield return "windows_graphics:no frame for 10 sec, try to restart";
        yield return "LauncherTask:launcher_button color 1.0";
        for (int index = 0; index < 200; index++) yield return "DailyRoutineTask:running routine item " + index;
    }

    private static IEnumerable<string> UpdateWaitStream()
    {
        yield return "LauncherTask:Launcher task started";
        yield return "upstream:LauncherTask._extend_deadline_for_update:waiting_for_official_update";
        for (int start = 0; start < 9; start++)
        {
            yield return "LauncherTask:Launcher task started";
            for (int tick = 0; tick < 5; tick++) yield return "windows_graphics:no frame for 10 sec, try to restart";
            for (int tick = 0; tick < 4; tick++) yield return "LauncherTask:launcher_button color 0.0";
            yield return "TaskExecutor:TaskDisabledException, continue LauncherTask";
        }
    }

    private static IEnumerable<string> StarvingStream(int starts, int buttonTicks, int frameTicks)
    {
        for (int start = 0; start < starts; start++)
        {
            yield return "LauncherTask:Launcher task started";
            for (int tick = 0; tick < frameTicks; tick++) yield return "windows_graphics:no frame for 10 sec, try to restart";
            for (int tick = 0; tick < buttonTicks; tick++) yield return "LauncherTask:launcher_button color 0.0";
            yield return "TaskExecutor:TaskDisabledException, continue LauncherTask";
        }
    }
}
