using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;

internal static class WwOfficialSourceCandidateReplay
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

    public static int Main(string[] args)
    {
        try { return Run(args); }
        catch (Exception error) { Console.Error.WriteLine(error.GetType().FullName); return 69; }
    }

    private static int Run(string[] args)
    {
        if (args == null || args.Length != 2) return 64;
        string root = Path.GetFullPath(args[0]);
        string runnerPath = Path.GetFullPath(args[1]);
        string source = Path.Combine(root, "WW", "source");
        string pythonw = Path.Combine(root, "r", "Scripts", "pythonw.exe");
        string main = Path.Combine(source, "main.py");
        string config = Path.Combine(source, "configs", "DailyTask.json");
        string archive = Path.Combine(root, "official-source.zip");
        Directory.CreateDirectory(Path.GetDirectoryName(pythonw));
        Directory.CreateDirectory(source);
        File.WriteAllText(pythonw, "fixture", new UTF8Encoding(false));
        File.WriteAllText(main, "# fixture", new UTF8Encoding(false));
        File.WriteAllText(archive, "fixture archive", new UTF8Encoding(false));
        // The first official launch is allowed to have no saved DailyTask
        // config.  The runner creates and later restores this temporary file.
        if (File.Exists(config)) return 65;
        string game = Path.Combine(root, "Wuthering Waves.exe");
        File.WriteAllText(game, "fixture", new UTF8Encoding(false));
        WriteManifest(root, archive, "main.py --task 1 --exit");

        Assembly runner = Assembly.LoadFrom(runnerPath);
        Type program = runner.GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        MethodInfo make = program.GetMethod("MakeFormalGuiBinding", BindingFlags.NonPublic | BindingFlags.Static);
        MethodInfo ensure = program.GetMethod("EnsureDailyConfigForInjection", BindingFlags.NonPublic | BindingFlags.Static);
        MethodInfo restore = program.GetMethod("RestoreDailyConfigSnapshot", BindingFlags.NonPublic | BindingFlags.Static);
        if (make == null || ensure == null || restore == null) return 66;
        object binding = Invoke(make, new object[] { "WW", game, root, null });
        Type bindingType = binding.GetType();
        if ((string)bindingType.GetField("Executable").GetValue(binding) != pythonw ||
            (string)bindingType.GetField("WorkingDirectory").GetValue(binding) != source ||
            (string)bindingType.GetField("Arguments").GetValue(binding) != "\"" + main + "\" --task 1 --exit" ||
            (string)bindingType.GetField("DailyConfig").GetValue(binding) != config ||
            (string)bindingType.GetField("AppName").GetValue(binding) != "ok-ww-official-source-candidate") return 67;
        Invoke(ensure, new object[] { config });
        if (!File.Exists(config) || File.ReadAllText(config, Encoding.UTF8) != "{}") return 68;
        Invoke(restore, new object[] { config, null, false });
        if (File.Exists(config)) return 69;

        WriteManifest(root, archive, "main.py --task 1 --exit", "07a578842b34dda3428535eb46f8b98f76fddb0d");
        Invoke(make, new object[] { "WW", game, root, null });
        WriteManifest(root, archive, "main.py --task 1 --exit", "36c557437de745cdef506bcfa148569d3e09cfcf");
        Invoke(make, new object[] { "WW", game, root, null });
        WriteManifest(root, archive, "main.py --task 1 --exit", "f5f38c8d9e2b7f7e91edad77bd5fab14df3f162d");
        Invoke(make, new object[] { "WW", game, root, null });
        WriteManifest(root, archive, "main.py --task 1 --exit", "20a2d40d0eaa0cfa053ceafecd9a4d04395291f4", "isolated-local-ww-source-patch", "f5f38c8d9e2b7f7e91edad77bd5fab14df3f162d");
        binding = Invoke(make, new object[] { "WW", game, root, null });
        if ((string)binding.GetType().GetField("AppName").GetValue(binding) != "ok-ww-local-source-patch-candidate") return 74;
        WriteManifest(root, archive, "main.py --task 1 --exit", "20a2d40d0eaa0cfa053ceafecd9a4d04395291f4", "isolated-local-ww-source-patch", "0000000000000000000000000000000000000000");
        try { Invoke(make, new object[] { "WW", game, root, null }); return 75; }
        catch (InvalidOperationException error) { if (error.Message != "configured_official_source_candidate_invalid") return 76; }
        WriteManifest(root, archive, "main.py --task 1 --exit", "20a2d40d0eaa0cfa053ceafecd9a4d04395291f4");
        try { Invoke(make, new object[] { "WW", game, root, null }); return 77; }
        catch (InvalidOperationException error) { if (error.Message != "configured_official_source_candidate_invalid") return 78; }
        WriteManifest(root, archive, "main.py --task 1 --exit", "0000000000000000000000000000000000000000");
        try { Invoke(make, new object[] { "WW", game, root, null }); return 72; }
        catch (InvalidOperationException error) { if (error.Message != "configured_official_source_candidate_invalid") return 73; }
        WriteManifest(root, archive, "arbitrary-command --unsafe");
        try { Invoke(make, new object[] { "WW", game, root, null }); return 70; }
        catch (InvalidOperationException error) { return error.Message == "configured_official_source_candidate_invalid" ? 0 : 71; }
    }

    private static object Invoke(MethodInfo method, params object[] values)
    {
        try { return method.Invoke(null, values); }
        catch (TargetInvocationException error) { throw error.InnerException; }
    }

    private static void WriteManifest(string root, string archive, string arguments, string commit = "c3fef9a06f61f96abeb53571fb47dd578b6f7063", string kind = "isolated-official-ww-source", string officialBase = "")
    {
        var document = new Dictionary<string, object> {
            { "schemaVersion", 1 }, { "status", "read-only-import-ready" },
            { "candidateKind", kind },
            { "source", new Dictionary<string, object> {
                { "repository", "https://github.com/ok-oldking/ok-wuthering-waves.git" },
                { "commit", commit }, { "officialBaseCommit", officialBase }, { "sourceDirectory", "WW/source" },
                { "archive", new Dictionary<string, object> { { "file", "official-source.zip" }, { "sha256", Hash(archive) } } }
            }},
            { "entry", new Dictionary<string, object> {
                { "pythonwRelativePath", "r\\Scripts\\pythonw.exe" }, { "workingDirectoryRelativePath", "WW/source" },
                { "sourceEntry", "main.py" }, { "arguments", arguments }
            }}
        };
        string path = Path.Combine(root, "WW", "runtime-preparation.json");
        Directory.CreateDirectory(Path.GetDirectoryName(path));
        File.WriteAllText(path, Json.Serialize(document), new UTF8Encoding(false));
    }

    private static string Hash(string path)
    {
        using (var stream = File.OpenRead(path)) using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", String.Empty).ToLowerInvariant();
    }
}
