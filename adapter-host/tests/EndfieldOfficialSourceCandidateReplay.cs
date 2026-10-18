using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;

internal static class EndfieldOfficialSourceCandidateReplay
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
        string source = Path.Combine(root, "Endfield", "source");
        string pythonw = Path.Combine(root, "r", "Scripts", "pythonw.exe");
        string main = Path.Combine(source, "main.py");
        string config = Path.Combine(source, "configs", "DailyTask.json");
        Directory.CreateDirectory(Path.GetDirectoryName(pythonw));
        Directory.CreateDirectory(Path.GetDirectoryName(config));
        File.WriteAllText(pythonw, "fixture", new UTF8Encoding(false));
        File.WriteAllText(main, "# fixture", new UTF8Encoding(false));
        File.WriteAllText(config, "{}", new UTF8Encoding(false));
        string game = Path.Combine(root, "Endfield.exe");
        File.WriteAllText(game, "fixture", new UTF8Encoding(false));
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit");

        Assembly runner = Assembly.LoadFrom(runnerPath);
        Type program = runner.GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        MethodInfo make = program.GetMethod("MakeFormalGuiBinding", BindingFlags.NonPublic | BindingFlags.Static);
        if (make == null) return 65;
        object binding = Invoke(make, new object[] { "Endfield", game, root, null });
        Type bindingType = binding.GetType();
        if ((string)bindingType.GetField("Executable").GetValue(binding) != pythonw ||
            (string)bindingType.GetField("WorkingDirectory").GetValue(binding) != source ||
            (string)bindingType.GetField("Arguments").GetValue(binding) != "\"" + main + "\" --task 1 --exit" ||
            (string)bindingType.GetField("DailyConfig").GetValue(binding) != config) return 66;

        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", "9b45815dcefdb3d4c273fdc26482782eb1e18395");
        binding = Invoke(make, new object[] { "Endfield", game, root, null });
        if ((string)bindingType.GetField("Executable").GetValue(binding) != pythonw) return 70;
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", "0815bdf3da08bcf5de0b0e0d55efbd7b7719ace4");
        binding = Invoke(make, new object[] { "Endfield", game, root, null });
        if ((string)bindingType.GetField("Executable").GetValue(binding) != pythonw) return 73;
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", "60bb694e63658adeeec83cc474a5a06626f25c6e");
        binding = Invoke(make, new object[] { "Endfield", game, root, null });
        if ((string)bindingType.GetField("Executable").GetValue(binding) != pythonw) return 83;
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", "83434dc40e2db364eb9dfa0fe9d0234bc4d7a164");
        binding = Invoke(make, new object[] { "Endfield", game, root, null });
        if ((string)bindingType.GetField("Executable").GetValue(binding) != pythonw) return 84;
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", "3d1d521f6fb367c8da2cd480511585fd219e69aa");
        binding = Invoke(make, new object[] { "Endfield", game, root, null });
        if ((string)bindingType.GetField("Executable").GetValue(binding) != pythonw) return 85;
        string patchCommit = "ec5058f90b91f816201227cd66b75770422c7c67";
        string patchKind = "isolated-local-source-patch-with-formal-runtime-binding";
        string officialBase = "34626b32e5cf5f8a7039b0d10c898169a5c421d6";
        string gameFlow = Path.Combine(source, "src", "core", "base_mixin", "game_flow_mixin.py");
        Directory.CreateDirectory(Path.GetDirectoryName(gameFlow));
        File.WriteAllText(gameFlow, "# audited fixture", new UTF8Encoding(false));
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", patchCommit, patchKind, officialBase);
        binding = Invoke(make, new object[] { "Endfield", game, root, null });
        if ((string)bindingType.GetField("AppName").GetValue(binding) != "ok-ef-local-source-patch-candidate") return 74;
        patchCommit = "19eb6d2015850b25f6d27c74edf27bf932567503";
        officialBase = "ef58c1c2d87e0f80a0075324e4797c1fbf267d5c";
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", patchCommit, patchKind, officialBase);
        binding = Invoke(make, new object[] { "Endfield", game, root, null });
        if ((string)bindingType.GetField("AppName").GetValue(binding) != "ok-ef-local-source-patch-candidate") return 78;
        patchCommit = "84e5df86197a68c4001f3a810b38c953aad60950";
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", patchCommit, patchKind, officialBase);
        binding = Invoke(make, new object[] { "Endfield", game, root, null });
        if ((string)bindingType.GetField("AppName").GetValue(binding) != "ok-ef-local-source-patch-candidate") return 79;
        patchCommit = "402b2da9bc46be6a2876f3f1ec4ed732754b636f";
        officialBase = "08af80be42ae11fdd482fbdb0b00f87f08cdf2e3";
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", patchCommit, patchKind, officialBase);
        binding = Invoke(make, new object[] { "Endfield", game, root, null });
        if ((string)bindingType.GetField("AppName").GetValue(binding) != "ok-ef-local-source-patch-candidate") return 80;
        File.AppendAllText(gameFlow, "# changed", new UTF8Encoding(false));
        if (!Rejected(make, game, root)) return 75;
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", patchCommit, patchKind, new string('0', 40));
        if (!Rejected(make, game, root)) return 76;
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit",
            "b69a1c12c6907ed0830320ae7433111ddea5e675", patchKind, "cfa1069a30eab05f9bc26ff571cfa7222b9d064e");
        if (!Rejected(make, game, root)) return 81;
        string creditCollect = Path.Combine(source, "src", "tasks", "onetime", "CreditCollectTask.py");
        Directory.CreateDirectory(Path.GetDirectoryName(creditCollect));
        File.WriteAllText(creditCollect, "# unverified credit implementation", new UTF8Encoding(false));
        if (!Rejected(make, game, root)) return 82;
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", patchCommit);
        if (!Rejected(make, game, root)) return 77;
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", new string('0', 40));
        try { Invoke(make, new object[] { "Endfield", game, root, null }); return 71; }
        catch (InvalidOperationException error) { if (error.Message != "configured_official_source_candidate_invalid") return 72; }
        WriteManifest(root, pythonw, source, main, "arbitrary-command --unsafe");
        try { Invoke(make, new object[] { "Endfield", game, root, null }); return 67; }
        catch (InvalidOperationException error) { return error.Message == "configured_official_source_candidate_invalid" ? 0 : 68; }
    }
    private static object Invoke(MethodInfo method, params object[] values)
    {
        try { return method.Invoke(null, values); }
        catch (TargetInvocationException error) { throw error.InnerException; }
    }
    private static bool Rejected(MethodInfo make, string game, string root)
    {
        try { Invoke(make, new object[] { "Endfield", game, root, null }); return false; }
        catch (InvalidOperationException error) { return error.Message == "configured_official_source_candidate_invalid"; }
    }
    private static void WriteManifest(string root, string pythonw, string source, string main, string args, string commit = "b13f1197a503e6473fbf63c1f927a33838739d1f", string kind = "isolated-official-source-with-formal-runtime-binding", string officialBase = null)
    {
        var document = new Dictionary<string, object> {
            { "schemaVersion", 1 }, { "status", "read-only-import-ready" },
            { "candidateKind", kind },
            { "source", new Dictionary<string, object> {
                { "repository", "https://github.com/AliceJump/ok-end-field.git" },
                { "commit", commit }, { "officialBaseCommit", officialBase }, { "sourceDirectory", "Endfield/source" },
                { "keyFileSha256", new Dictionary<string, object> { { "main", Hash(main) } } }
            }},
            { "entry", new Dictionary<string, object> {
                { "pythonw", pythonw }, { "workingDirectory", source }, { "sourceEntry", "main.py" }, { "arguments", args }
            }}
        };
        string gameFlow = Path.Combine(source, "src", "core", "base_mixin", "game_flow_mixin.py");
        if (File.Exists(gameFlow)) {
            var hashes = (Dictionary<string, object>)((Dictionary<string, object>)document["source"])["keyFileSha256"];
            hashes["gameFlow"] = Hash(gameFlow);
        }
        string path = Path.Combine(root, "Endfield", "runtime-preparation.json");
        Directory.CreateDirectory(Path.GetDirectoryName(path));
        File.WriteAllText(path, Json.Serialize(document), new UTF8Encoding(false));
    }
    private static string Hash(string path)
    {
        using (var stream = File.OpenRead(path)) using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", String.Empty).ToLowerInvariant();
    }
}
