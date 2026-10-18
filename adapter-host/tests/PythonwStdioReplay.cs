using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

// Official pythonw must leave CPython bootstrap when launched the same way
// RunDaily does. A still-open inherited Host stdin pipe is the production hang.
internal static class PythonwStdioReplay
{
    static int Main(string[] args)
    {
        Type capture = Assembly.LoadFrom(args[0]).GetType("YeYuGamer.AdapterHost.UpstreamConsoleCapture", true);
        MethodInfo configure = capture.GetMethod("Configure", BindingFlags.Static | BindingFlags.NonPublic);
        MethodInfo drain = capture.GetMethod("Drain", BindingFlags.Static | BindingFlags.NonPublic);
        string pythonw = args[1];
        string work = args[2];
        if (!File.Exists(pythonw)) throw new Exception("pythonw missing: " + pythonw);
        Directory.CreateDirectory(work);
        string probe = Path.Combine(work, "probe.py");
        string marker = Path.Combine(work, "probe.alive");
        string logPath = Path.Combine(work, "probe-console.jsonl");
        File.WriteAllText(probe, "from pathlib import Path" + Environment.NewLine +
            "Path(r'" + marker.Replace("\\", "\\\\") + "').write_text('ok', encoding='utf-8')" + Environment.NewLine, new UTF8Encoding(false));
        if (File.Exists(marker)) File.Delete(marker);
        var start = new ProcessStartInfo
        {
            FileName = pythonw,
            Arguments = "\"" + probe + "\"",
            WorkingDirectory = work,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        configure.Invoke(null, new object[] { start });
        using (Process child = Process.Start(start))
        {
            if (child == null) throw new Exception("pythonw_not_started");
            drain.Invoke(null, new object[] { child, logPath });
            if (!child.WaitForExit(8000))
            {
                try { child.Kill(); } catch { }
                throw new Exception("pythonw_stdio_bootstrap_blocked");
            }
            child.WaitForExit();
            if (child.ExitCode != 0) throw new Exception("pythonw_probe_exit=" + child.ExitCode);
        }
        if (!File.Exists(marker)) throw new Exception("pythonw_probe_did_not_run");
        Console.WriteLine("{\"suite\":\"pythonw-stdio\",\"passed\":true,\"gameStarted\":false}");
        return 0;
    }
}
