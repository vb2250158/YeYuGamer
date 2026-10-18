using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Web.Script.Serialization;

// Exercise the compiled runner's real redirected-stream hooks with an offline
// child, including long lines, EOF without newline and observation I/O failure.
internal static class OpenKuroConsoleReplay
{
    static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--emit") {
            Console.OutputEncoding = new UTF8Encoding(false);
            Console.WriteLine("bootstrap-start");
            Console.WriteLine(new string('x', 100000));
            Console.Error.Write("stderr-final-without-newline");
            Console.Write("stdout-final-without-newline");
            return 17;
        }
        Type program = Assembly.LoadFrom(args[0]).GetType("YeYuGamer.AdapterHost.UpstreamConsoleCapture", true);
        MethodInfo configure = program.GetMethod("Configure", BindingFlags.Static | BindingFlags.NonPublic);
        MethodInfo drain = program.GetMethod("Drain", BindingFlags.Static | BindingFlags.NonPublic);
        string logPath = args[1];
        Run(configure, drain, logPath);
        var json = new JavaScriptSerializer { MaxJsonLength = Int32.MaxValue };
        var rows = File.ReadAllLines(logPath).Select(line => (IDictionary<string, object>)json.DeserializeObject(line)).ToArray();
        if (rows.Length != 4 ||
            !rows.Any(row => (string)row["message"] == new string('x', 100000)) ||
            !rows.Any(row => (string)row["logger"] == "stderr" && (string)row["message"] == "stderr-final-without-newline") ||
            !rows.Any(row => (string)row["logger"] == "stdout" && (string)row["message"] == "stdout-final-without-newline"))
            throw new Exception("Console capture dropped or truncated tool output");
        // A directory cannot be appended as a file. Capture failure must not
        // deadlock the child, alter its exit code, or escape the observer.
        Run(configure, drain, Path.GetDirectoryName(logPath));
        Console.WriteLine("{\"suite\":\"upstream-console\",\"passed\":true,\"records\":4,\"gameStarted\":false}");
        return 0;
    }

    static void Run(MethodInfo configure, MethodInfo drain, string logPath)
    {
        var start = new ProcessStartInfo {
            FileName = Assembly.GetExecutingAssembly().Location, Arguments = "--emit",
            UseShellExecute = false, CreateNoWindow = true
        };
        configure.Invoke(null, new object[] { start });
        using (Process child = Process.Start(start)) {
            drain.Invoke(null, new object[] { child, logPath });
            if (!child.WaitForExit(10000)) { child.Kill(); throw new Exception("Console pipe stalled"); }
            child.WaitForExit();
            if (child.ExitCode != 17) throw new Exception("Observer changed child outcome");
        }
    }
}
