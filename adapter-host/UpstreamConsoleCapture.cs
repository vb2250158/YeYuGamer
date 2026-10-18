using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

namespace YeYuGamer.AdapterHost
{
    // Collect bootstrap output that may precede an upstream logger. The caller
    // waits for process exit (including async EOF) before sealing the log.
    internal static class UpstreamConsoleCapture
    {
        // A Windows environment block may carry two entries whose names differ
        // only by case ("Path" beside "PATH").  ProcessStartInfo publishes the
        // inherited block through a case-insensitive StringDictionary, so the
        // first EnvironmentVariables access throws ArgumentException and the
        // runner never reaches its official tool.  Collapse each case-folded
        // group onto one spelling before any ProcessStartInfo reads the
        // environment; an already clean block is returned unchanged.
        internal static void NormalizeInheritedEnvironment()
        {
            var firstValues = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var duplicates = new List<string>();
            foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables())
            {
                string name = entry.Key as string;
                if (name == null) continue;
                if (!firstValues.ContainsKey(name)) firstValues[name] = entry.Value as string ?? String.Empty;
                else duplicates.Add(name);
            }
            if (duplicates.Count == 0) return;
            foreach (string name in duplicates)
            {
                try { Environment.SetEnvironmentVariable(name, null); } catch { }
            }
            // Windows matches names case-insensitively, so re-applying the first
            // spelling's value under the duplicate's spelling leaves one entry.
            foreach (string name in duplicates)
            {
                string value;
                if (!firstValues.TryGetValue(name, out value)) continue;
                try { Environment.SetEnvironmentVariable(name, value); } catch { }
            }
        }

        internal static void Configure(ProcessStartInfo start)
        {
            NormalizeInheritedEnvironment();
            // Host.exe keeps the runner's stdin pipe open after delivering the
            // execute request. A pythonw child that inherits that pipe blocks
            // inside CPython stdio bootstrap (UTF-8 BOM read) and never loads
            // past python312.dll. Redirect stdin here so Drain can close it.
            start.RedirectStandardInput = true;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            start.StandardOutputEncoding = Encoding.UTF8;
            start.StandardErrorEncoding = Encoding.UTF8;
            start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        }

        internal static void Drain(Process process, string logPath)
        {
            if (process == null) return;
            try
            {
                if (process.StartInfo.RedirectStandardInput)
                    process.StandardInput.Close();
            }
            catch { }
            var outputLock = new object();
            var serializer = new JavaScriptSerializer { MaxJsonLength = Int32.MaxValue };
            Action<string, string> record = (channel, line) =>
            {
                if (line == null) return;
                try
                {
                    lock (outputLock)
                    {
                        File.AppendAllText(logPath, serializer.Serialize(new Dictionary<string, object>
                        {
                            { "timestamp", DateTime.UtcNow.ToString("o") },
                            { "logger", channel }, { "message", line }
                        }) + Environment.NewLine, new UTF8Encoding(false));
                    }
                }
                catch { } // Reporting failure never changes the official tool.
            };
            process.OutputDataReceived += (sender, args) => record("stdout", args.Data);
            process.ErrorDataReceived += (sender, args) => record("stderr", args.Data);
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
        }
    }
}
