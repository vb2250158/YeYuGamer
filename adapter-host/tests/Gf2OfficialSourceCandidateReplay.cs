using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;

internal static class Gf2OfficialSourceCandidateReplay
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    public static int Main(string[] args)
    {
        try { return Run(args); }
        catch (Exception error) { Console.Error.WriteLine(error.GetType().FullName); return 69; }
    }
    private static int Run(string[] args)
    {
        if (args == null || (args.Length != 2 && args.Length != 3)) return 64;
        string root = Path.GetFullPath(args[0]);
        string runnerPath = Path.GetFullPath(args[1]);
        string source = Path.Combine(root, "GF2", "source");
        string pythonw = Path.Combine(root, "r", "Scripts", "pythonw.exe");
        string main = Path.Combine(source, "main.py");
        string config = Path.Combine(source, "configs", "DailyTask.json");
        Directory.CreateDirectory(Path.GetDirectoryName(pythonw));
        Directory.CreateDirectory(Path.GetDirectoryName(config));
        File.WriteAllText(pythonw, "fixture", new UTF8Encoding(false));
        File.WriteAllText(main, "# fixture", new UTF8Encoding(false));
        File.WriteAllText(config, "{}", new UTF8Encoding(false));
        string game = Path.Combine(root, "GF2_Exilium.exe");
        File.WriteAllText(game, "fixture", new UTF8Encoding(false));
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit");

        Assembly runner = Assembly.LoadFrom(runnerPath);
        Type program = runner.GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        MethodInfo make = program.GetMethod("MakeFormalGuiBinding", BindingFlags.NonPublic | BindingFlags.Static);
        if (make == null) return 65;
        object binding = Invoke(make, new object[] { "GF2", game, root, null });
        Type bindingType = binding.GetType();
        if ((string)bindingType.GetField("Executable").GetValue(binding) != pythonw ||
            (string)bindingType.GetField("WorkingDirectory").GetValue(binding) != source ||
            (string)bindingType.GetField("Arguments").GetValue(binding) != "\"" + main + "\" --task 1 --exit" ||
            (string)bindingType.GetField("DailyConfig").GetValue(binding) != config ||
            (bool)bindingType.GetField("Gf2SupplyRewardChoiceSupported").GetValue(binding)) return 66;

        string staging = Path.Combine(root, "staging");
        Directory.CreateDirectory(staging);
        string managerBinding = Path.Combine(staging, "installation-binding.json");
        File.WriteAllText(managerBinding, Json.Serialize(new Dictionary<string, object> {
            { "schemaVersion", 2 }, { "gameId", "GF2" }, { "gamePath", game }, { "toolPath", root },
            { "dailyTaskProfile", new Dictionary<string, object> {
                { "autoBattleConfirmed", null }, { "staminaStage", null }, { "extraCreditBattle", false },
                { "autonomousLoop", null }, { "squadDust", null }, { "eventStageName", null },
                { "drinkDurations", null }, { "mealDuration", null }
            }}
        }), new UTF8Encoding(false));
        string previousStaging = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
        try {
            Environment.SetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR", staging);
            MethodInfo loadManager = program.GetMethod("LoadManagerBinding", BindingFlags.NonPublic | BindingFlags.Static);
            if (loadManager == null) return 67;
            object managerLoaded = Invoke(loadManager, new object[] { managerBinding, "GF2" });
            IDictionary<string, object> profile = bindingType.GetField("DailyTaskProfile").GetValue(managerLoaded) as IDictionary<string, object>;
            if (profile == null || profile.Count != 8 || !(profile["extraCreditBattle"] is bool) || (bool)profile["extraCreditBattle"]) return 68;
        } finally { Environment.SetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR", previousStaging); }

        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit",
            "https://github.com/AliceJump/ok-gf2.git", "5f19a91bed0c98d3cbd919e40cbfcfe0e34b3f42", "v1.2.77");
        object latest = Invoke(make, new object[] { "GF2", game, root, null });
        if (!(bool)bindingType.GetField("Gf2SupplyRewardChoiceSupported").GetValue(latest)) return 76;
        if ((string)bindingType.GetField("Executable").GetValue(latest) != pythonw ||
            (string)bindingType.GetField("Arguments").GetValue(latest) != "\"" + main + "\" --task 1 --exit") return 71;
        WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit",
            "https://github.com/AliceJump/ok-gf2.git", "0fae94eb01621c00fba7078bb3e7316a95185c5d", "v1.2.81");
        object release1281 = Invoke(make, new object[] { "GF2", game, root, null });
        if (!(bool)bindingType.GetField("Gf2SupplyRewardChoiceSupported").GetValue(release1281)) return 77;
        if (args.Length == 3) {
            string audited = Path.GetFullPath(args[2]);
            foreach (string relative in new[] { "src/core/BaseGfTask.py", "src/core/resource_update.py", "tests/TestResourceUpdate.py" }) {
                string destination = Path.Combine(source, relative.Replace('/', Path.DirectorySeparatorChar));
                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                File.Copy(Path.Combine(audited, relative.Replace('/', Path.DirectorySeparatorChar)), destination, true);
            }
            WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit",
                "https://github.com/AliceJump/ok-gf2.git", "f70efd718b220d8b86085f7652b340e1124bae80", "");
            string patchRecordPath = Path.Combine(root, "GF2", "runtime-preparation.json");
            var patchRecord = Json.DeserializeObject(File.ReadAllText(patchRecordPath)) as IDictionary<string, object>;
            patchRecord["candidateKind"] = "isolated-local-gf2-source-patch-with-formal-runtime-binding";
            var patchSource = patchRecord["source"] as IDictionary<string, object>;
            patchSource["tag"] = null;
            patchSource["officialBaseCommit"] = "6b2a194b7f6371b5d8d2b33ed62809272f481db3";
            File.WriteAllText(patchRecordPath, Json.Serialize(patchRecord), new UTF8Encoding(false));
            Invoke(make, new object[] { "GF2", game, root, null });
            string helper = Path.Combine(source, "src", "core", "resource_update.py");
            File.AppendAllText(helper, "\n# unexpected modification", new UTF8Encoding(false));
            try { Invoke(make, new object[] { "GF2", game, root, null }); return 78; }
            catch (InvalidOperationException error) { if (error.Message != "configured_official_source_candidate_invalid") return 79; }
        }
        foreach (string[] tuple in new[] {
            new[] { "https://github.com/unknown/ok-gf2.git", "5f19a91bed0c98d3cbd919e40cbfcfe0e34b3f42", "v1.2.77" },
            new[] { "https://github.com/AliceJump/ok-gf2.git", "4122e01e8a69063ae37807f316f2ae8f80eeedb1", "v1.2.77" },
            new[] { "https://github.com/AliceJump/ok-gf2.git", "5f19a91bed0c98d3cbd919e40cbfcfe0e34b3f42", "v1.2.76" }
        }) {
            WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit", tuple[0], tuple[1], tuple[2]);
            try { Invoke(make, new object[] { "GF2", game, root, null }); return 72; }
            catch (InvalidOperationException error) { if (error.Message != "configured_official_source_candidate_invalid") return 73; }
        }
        string recordPath = Path.Combine(root, "GF2", "runtime-preparation.json");
        foreach (bool tamperedFile in new[] { false, true }) {
            WriteManifest(root, pythonw, source, main, "main.py --task 1 --exit",
                "https://github.com/AliceJump/ok-gf2.git", "5f19a91bed0c98d3cbd919e40cbfcfe0e34b3f42", "v1.2.77");
            var record = Json.DeserializeObject(File.ReadAllText(recordPath)) as IDictionary<string, object>;
            record["sdkSourcePatch"] = new Dictionary<string, object> {
                { "repository", "https://github.com/ok-oldking/ok-script.git" },
                { "officialBaseCommit", "752e01d02c9e298a3128f11d9ec0ed9c654dd0e6" },
                { "commit", "dabd9f7f99c2dec7ce1ff90a6f84576e0dc99ca4" },
                { "version", "2.0.8+local.genshincleanup.20261003" },
                { "candidateKind", "local-upstream-sdk-source-patch" },
                { "genshinSha256", "a23a384e038dfb012ef7d4dbba76d0bdbaf4e13b993efdaef9a6a2d74cbb5cd8" }
            };
            File.WriteAllText(recordPath, Json.Serialize(record), new UTF8Encoding(false));
            if (tamperedFile) {
                string sdkFile = Path.Combine(root, "r", "Lib", "site-packages", "ok", "device", "interaction_methods", "genshin.py");
                Directory.CreateDirectory(Path.GetDirectoryName(sdkFile));
                File.WriteAllText(sdkFile, "# changed SDK", new UTF8Encoding(false));
            }
            try { Invoke(make, new object[] { "GF2", game, root, null }); return 74; }
            catch (InvalidOperationException error) { if (error.Message != "configured_official_sdk_candidate_invalid") return 75; }
        }
        WriteManifest(root, pythonw, source, main, "arbitrary-command --unsafe");
        try { Invoke(make, new object[] { "GF2", game, root, null }); return 69; }
        catch (InvalidOperationException error) { return error.Message == "configured_official_source_candidate_invalid" ? 0 : 70; }
    }
    private static object Invoke(MethodInfo method, params object[] values)
    {
        try { return method.Invoke(null, values); }
        catch (TargetInvocationException error) { throw error.InnerException; }
    }
    private static void WriteManifest(string root, string pythonw, string source, string main, string args,
        string repository = "https://cnb.cool/ok-oldking/ok-gf2.git",
        string commit = "4122e01e8a69063ae37807f316f2ae8f80eeedb1", string tag = "")
    {
        var document = new Dictionary<string, object> {
            { "schemaVersion", 1 }, { "status", "read-only-import-ready" },
            { "candidateKind", "isolated-official-gf2-source" },
            { "source", new Dictionary<string, object> {
                { "repository", repository },
                { "commit", commit }, { "tag", tag }, { "sourceDirectory", "GF2/source" },
                { "keyFileSha256", new Dictionary<string, object> { { "main", Hash(main) } } }
            }},
            { "entry", new Dictionary<string, object> {
                { "pythonw", pythonw }, { "workingDirectory", source }, { "sourceEntry", "main.py" }, { "arguments", args }
            }}
        };
        string path = Path.Combine(root, "GF2", "runtime-preparation.json");
        Directory.CreateDirectory(Path.GetDirectoryName(path));
        File.WriteAllText(path, Json.Serialize(document), new UTF8Encoding(false));
    }
    private static string Hash(string path)
    {
        using (var stream = File.OpenRead(path)) using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", String.Empty).ToLowerInvariant();
    }
}
