using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;
using System.Web.Script.Serialization;

// Exercise production snapshot, projection and recovery without starting a tool.
internal static class EndfieldSplitConfigReplay {
    static int Main(string[] args) {
        try { return Run(args); }
        catch (Exception error) {
            while (error is TargetInvocationException && error.InnerException != null) error = error.InnerException;
            Console.Error.WriteLine(error.GetType().FullName + " hresult=" + error.HResult.ToString("X8"));
            Console.Error.WriteLine(error.Message);
            Console.Error.WriteLine(error.StackTrace);
            return 1;
        }
    }
    static int Run(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        Type program = Assembly.LoadFrom(args[0]).GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        Type bindingType = program.GetNestedType("Binding", BindingFlags.NonPublic);
        object binding = Activator.CreateInstance(bindingType, true);
        string root = args[1], config = Path.Combine(root, "configs");
        bindingType.GetField("GameId").SetValue(binding, "Endfield");
        bindingType.GetField("WorkingDirectory").SetValue(binding, root);
        bindingType.GetField("DailyConfig").SetValue(binding, Path.Combine(config, "DailyTask.json"));
        bindingType.GetField("Executable").SetValue(binding, Path.Combine(root, "fixture-not-running.exe"));
        var json = new JavaScriptSerializer();
        var flags = BindingFlags.Static | BindingFlags.NonPublic;
        object snapshots;
        try { snapshots = program.GetMethod("SnapshotEndfieldSplitConfig", flags).Invoke(null, new[] { binding }); }
        catch (TargetInvocationException error) {
            if (args[2] != "reject-migration") throw;
            Console.WriteLine(error.InnerException.Message);
            return 0;
        }
        if (args[2] == "reject-migration") throw new Exception("unmigrated config was accepted");
        string[] selected = args[3].Length == 0 ? new string[0] : args[3].Split(',');
        if (args.Length > 4) {
            bindingType.GetField("DailyTaskProfile").SetValue(binding,
                json.DeserializeObject(File.ReadAllText(args[4])) as IDictionary<string, object>);
            program.GetMethod("ApplyEndfieldDailyTaskProfile", flags).Invoke(null, new[] { binding });
        }
        program.GetMethod("ApplySelectedOfficialDailyConfig", flags).Invoke(null, new object[] { binding, selected });
        var projected = new Dictionary<string, object>();
        foreach (string path in Directory.GetFiles(config, "*.json"))
            projected[Path.GetFileNameWithoutExtension(path)] = json.DeserializeObject(File.ReadAllText(path));
        Console.WriteLine(json.Serialize(projected));
        // Simulate an unverified live child: retain recovery records instead
        // of writing original configurations under that child.
        if (args[2] == "pending") {
            program.GetMethod("RestoreEndfieldSplitConfig", flags).Invoke(null, new[] { snapshots, (object)false });
        } else {
            program.GetMethod("RestoreEndfieldSplitConfig", flags).Invoke(null, new[] { snapshots, (object)true });
        }
        return 0;
    }
}
