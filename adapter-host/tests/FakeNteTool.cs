using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

internal static class FakeNteTool
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

    public static int Main(string[] args)
    {
        if (args != null && args.Length == 1 && args[0] == "--version")
        {
            Console.Out.WriteLine("Python 3.12.99");
            return 0;
        }
        if (args != null && args.Length >= 2 && args[0] == "-c") return 0;
        string stagePath = Environment.GetEnvironmentVariable("YEYU_GAMER_STAGE_FILE");
        string selectedText = Environment.GetEnvironmentVariable("YEYU_GAMER_SELECTED_OPERATIONS");
        string profileText = Environment.GetEnvironmentVariable("YEYU_GAMER_NTE_PROFILE");
        if (String.IsNullOrWhiteSpace(stagePath) || String.IsNullOrWhiteSpace(selectedText) ||
            String.IsNullOrWhiteSpace(profileText)) return 70;
        object[] selected;
        try { selected = Json.DeserializeObject(selectedText) as object[]; }
        catch { return 71; }
        if (selected == null || selected.Length == 0) return 72;

        string root = Directory.GetCurrentDirectory();
        File.AppendAllText(Path.Combine(root, "invocations.log"), String.Join(" ", args) + Environment.NewLine, new UTF8Encoding(false));
        File.AppendAllText(Path.Combine(root, "profiles.log"), profileText + Environment.NewLine, new UTF8Encoding(false));
        string modePath = Path.Combine(root, "fake-mode.txt");
        string mode = File.Exists(modePath) ? File.ReadAllText(modePath, Encoding.UTF8).Trim() : "complete";
        Console.Out.WriteLine("fake tool stdout is captured by runner");
        Console.Error.WriteLine("fake tool stderr is captured by runner");

        for (int index = 0; index < selected.Length; index++)
        {
            string operation = selected[index] as string;
            if (String.IsNullOrWhiteSpace(operation)) return 73;
            if (mode == "missing" && index == selected.Length - 1) continue;
            Write(stagePath, operation, "started", "fake selected stage");
            if (mode == "failed" && index == 0)
            {
                Write(stagePath, operation, "failed", "fake failure");
                return 4;
            }
            Write(stagePath, operation, "completed", "fake completion");
        }
        return 0;
    }

    private static void Write(string path, string operation, string state, string detail)
    {
        var record = new Dictionary<string, object>
        {
            { "operation", operation }, { "state", state }, { "detail", detail },
            { "at", DateTime.UtcNow.ToString("o") }
        };
        File.AppendAllText(path, Json.Serialize(record) + Environment.NewLine, new UTF8Encoding(false));
    }
}
