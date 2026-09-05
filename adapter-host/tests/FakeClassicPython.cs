using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

internal static class FakeClassicPython
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

    public static int Main()
    {
        string stage = Environment.GetEnvironmentVariable("YEYU_GAMER_STAGE_FILE");
        string raw = Environment.GetEnvironmentVariable("YEYU_GAMER_SELECTED_OPERATIONS");
        string mode = Environment.GetEnvironmentVariable("CLASSIC_FAKE_MODE") ?? "completed";
        if (String.IsNullOrEmpty(stage) || String.IsNullOrEmpty(raw)) return 64;
        object[] operations = Json.DeserializeObject(raw) as object[];
        if (operations == null || operations.Length == 0) return 64;
        if (mode == "missing") return 0;
        foreach (object item in operations)
        {
            string operation = item as string;
            Append(stage, operation, "started", "fake selected tool stage");
            Append(stage, operation, mode == "failed" ? "failed" : "completed", "伪造回放结果 fake replay result");
        }
        return mode == "failed" ? 20 : 0;
    }

    private static void Append(string path, string operation, string state, string detail)
    {
        string line = Json.Serialize(new Dictionary<string, object> {
            { "operation", operation }, { "state", state }, { "detail", detail }
        });
        File.AppendAllText(path, line + "\n", new UTF8Encoding(false));
    }
}
