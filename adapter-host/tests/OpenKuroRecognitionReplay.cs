using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Web.Script.Serialization;

// Exercise production recognition/metadata importing without starting a tool.
internal static class OpenKuroRecognitionReplay
{
    static int Main(string[] args)
    {
        Type program = Assembly.LoadFrom(args[0]).GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        BindingFlags flags = BindingFlags.Static | BindingFlags.NonPublic;
        MethodInfo allowed = program.GetMethod("IsStepArtifactKind", flags);
        MethodInfo mime = program.GetMethod("StepArtifactMimeType", flags);
        MethodInfo import = program.GetMethod("ImportWwSidecarArtifact", flags);
        foreach (string kind in new[] { "game-ui-recognition-raw", "game-ui-recognition-replay-raw", "official-recognition-metadata", "game-ui-step-before-raw" })
            if (!(bool)allowed.Invoke(null, new object[] { kind })) throw new Exception("Recognition/historical kind rejected");
        if ((bool)allowed.Invoke(null, new object[] { "game-ui-reward-verification-raw" }))
            throw new Exception("Generic recognition input became reward proof");
        if ((string)mime.Invoke(null, new object[] { "official-recognition-metadata" }) != "text/plain" ||
            (string)mime.Invoke(null, new object[] { "game-ui-recognition-raw" }) != "image/png" ||
            (string)mime.Invoke(null, new object[] { "game-ui-recognition-replay-raw" }) != "image/png")
            throw new Exception("Recognition artifact MIME type is wrong");
        string root = Path.GetFullPath(args[1]);
        string source = Path.Combine(root, "source");
        string staging = Path.Combine(root, "staging");
        Directory.CreateDirectory(source);
        Directory.CreateDirectory(staging);
        string previous = Environment.GetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR");
        Environment.SetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR", staging);
        try {
            var evidence = new Dictionary<string, List<Tuple<string, string, string>>>();
            string observedAt = DateTime.Now.AddHours(-1).ToString("o");
            string originalMetadata = "{\"observedAt\":\"" + observedAt + "\",\"capturedAt\":\"" + observedAt + "\",\"recognizerInputSha256\":\"original-input-hash\"}";
            File.WriteAllText(Path.Combine(source, "recognition.json"), originalMetadata);
            string detail = "file=recognition.json; kind=official-recognition-metadata; capturedAt=" + observedAt;
            object[] inputs = { Path.Combine(source, "stage.jsonl"), "claim-daily-reward", "step_artifact", detail, evidence };
            import.Invoke(null, inputs);
            import.Invoke(null, inputs);
            if (evidence["claim-daily-reward"].Count != 1 || evidence["claim-daily-reward"][0].Item3 != observedAt)
                throw new Exception("Import changed observed time or duplicated metadata");
            if (File.ReadAllText(Path.Combine(staging, "recognition.json")) != File.ReadAllText(Path.Combine(source, "recognition.json")))
                throw new Exception("Imported recognition metadata was rewritten");
            TestReplay(program, flags, source, staging, observedAt, originalMetadata);
            TestInspectionScope(program, flags, staging, observedAt);
            TestRewardStatusScope(program, flags, staging, observedAt);
            TestBattlePassStatusScope(program, flags, staging, observedAt);
        } finally {
            Environment.SetEnvironmentVariable("YEYU_GAMER_ADAPTER_STAGING_DIR", previous);
        }
        Console.WriteLine("{\"suite\":\"openkuro-recognition\",\"passed\":true,\"gameStarted\":false}");
        return 0;
    }

    static void TestRewardStatusScope(Type program, BindingFlags flags, string staging, string observedAt)
    {
        Type bindingType = program.GetNestedType("Binding", BindingFlags.NonPublic);
        object binding = Activator.CreateInstance(bindingType, true);
        bindingType.GetField("WwRewardInspectionSupported").SetValue(binding, true);
        bindingType.GetField("WwRewardInspectionOnly").SetValue(binding, true);
        bindingType.GetField("AccountId").SetValue(binding, "fixture-owner");
        bindingType.GetField("AccountSnapshot").SetValue(binding, new Dictionary<string, object> {{"saved_account_label", "fixture-saved-account"}});
        string stem = "ww-recognition-fedcba0987654321fedcba0987654321";
        string image = Path.Combine(staging, stem + "_original.png");
        File.WriteAllBytes(image, Convert.FromBase64String("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="));
        var json = new JavaScriptSerializer();
        var metadata = new Dictionary<string, object> {
            {"operation", "claim-daily-reward"}, {"recognitionType", "template"}, {"template", "daily_reward_claimed_check"},
            {"coordinateSpace", "full-frame"}, {"upstreamConfirmedAccount", "fixture-saved-account"}, {"observedAt", observedAt},
            {"screenshotSha256", program.GetMethod("Hash", flags).Invoke(null, new object[] {image})}
        };
        string metadataPath = Path.Combine(staging, stem + ".json");
        File.WriteAllText(metadataPath, json.Serialize(metadata));
        var request = new Dictionary<string, object> {{"runId", "fixture-run"}, {"runAttemptId", "fixture-attempt"}};
        object[] inputs = {binding, request, "fixture-todo", "fixture-todo-attempt", image, observedAt, "claim-daily-reward"};
        MethodInfo write = program.GetMethod("WriteWwVerificationScope", flags);
        write.Invoke(null, inputs);
        var scope = json.DeserializeObject(File.ReadAllText(image + ".recognition.json")) as IDictionary<string, object>;
        if ((string)scope["kind"] != "game-ui-reward-verification-raw" || (string)scope["boundary"] != "DailyTask.inspect_daily_reward_status")
            throw new Exception("Reward status scope changed its observation semantics");
        // Deferred terminals are flushed after every fixed inspection exits.
        // A later entry restoring its config cannot discard earlier scope.
        bindingType.GetField("WwRewardInspectionOnly").SetValue(binding, false);
        bindingType.GetField("WwInspectionOperations").SetValue(binding,
            new HashSet<string>(new[] { "claim-daily-reward" }, StringComparer.Ordinal));
        write.Invoke(null, inputs);
        bindingType.GetField("WwRewardInspectionSupported").SetValue(binding, false);
        try { write.Invoke(null,inputs); throw new Exception("Frozen scope accepted an unpinned inspection"); }
        catch (TargetInvocationException error) { if (!(error.InnerException is InvalidDataException)) throw; }
        bindingType.GetField("WwRewardInspectionSupported").SetValue(binding, true);
        metadata["template"] = "other-template";
        File.WriteAllText(metadataPath, json.Serialize(metadata));
        try { write.Invoke(null, inputs); throw new Exception("Reward status accepted unrelated template metadata"); }
        catch (TargetInvocationException error) { if (!(error.InnerException is InvalidDataException)) throw; }
        MethodInfo select = program.GetMethod("WwShouldUseRewardInspection", flags);
        if (!(bool)select.Invoke(null, new object[] {new[] {"attach-world", "claim-daily-reward"}, true}) ||
            (bool)select.Invoke(null, new object[] {new[] {"claim-daily-reward", "spend-waveplates"}, true}) ||
            (bool)select.Invoke(null, new object[] {new[] {"claim-daily-reward"}, false}))
            throw new Exception("Reward status selection widened the official task body");
    }

    static void TestBattlePassStatusScope(Type program, BindingFlags flags, string staging, string observedAt)
    {
        Type bindingType = program.GetNestedType("Binding", BindingFlags.NonPublic);
        object binding = Activator.CreateInstance(bindingType, true);
        bindingType.GetField("WwBattlePassInspectionSupported").SetValue(binding, true);
        bindingType.GetField("WwBattlePassInspectionOnly").SetValue(binding, true);
        bindingType.GetField("AccountId").SetValue(binding, "fixture-owner");
        bindingType.GetField("AccountSnapshot").SetValue(binding, new Dictionary<string, object> {{"saved_account_label", "fixture-saved-account"}});
        string stem = "ww-recognition-abcdef0123456789abcdef0123456789";
        string image = Path.Combine(staging, stem + "_original.png");
        File.WriteAllBytes(image, Convert.FromBase64String("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="));
        var json = new JavaScriptSerializer();
        var metadata = new Dictionary<string, object> {
            {"operation", "claim-battle-pass"}, {"recognitionType", "ocr"}, {"verificationPage", "tasks"},
            {"coordinateSpace", "full-frame"}, {"upstreamConfirmedAccount", "fixture-saved-account"}, {"observedAt", observedAt},
            {"screenshotSha256", program.GetMethod("Hash", flags).Invoke(null, new object[] {image})}
        };
        string metadataPath = Path.Combine(staging, stem + ".json");
        File.WriteAllText(metadataPath, json.Serialize(metadata));
        var request = new Dictionary<string, object> {{"runId", "fixture-run"}, {"runAttemptId", "fixture-attempt"}};
        object[] inputs = {binding, request, "fixture-todo", "fixture-todo-attempt", image, observedAt, "claim-battle-pass"};
        MethodInfo write = program.GetMethod("WriteWwVerificationScope", flags);
        write.Invoke(null, inputs);
        var scope = json.DeserializeObject(File.ReadAllText(image + ".recognition.json")) as IDictionary<string, object>;
        if ((string)scope["kind"] != "game-ui-reward-verification-raw" || (string)scope["boundary"] != "DailyTask.inspect_battle_pass_page" || (string)scope["verificationPage"] != "tasks")
            throw new Exception("Battle pass scope changed its observation semantics");
        metadata["verificationPage"] = "other-page";
        File.WriteAllText(metadataPath, json.Serialize(metadata));
        try { write.Invoke(null, inputs); throw new Exception("Battle pass accepted unrelated page metadata"); }
        catch (TargetInvocationException error) { if (!(error.InnerException is InvalidDataException)) throw; }
        MethodInfo select = program.GetMethod("WwShouldUseBattlePassInspection", flags);
        if (!(bool)select.Invoke(null, new object[] {new[] {"attach-world", "claim-battle-pass"}, true}) ||
            (bool)select.Invoke(null, new object[] {new[] {"claim-battle-pass", "spend-waveplates"}, true}) ||
            (bool)select.Invoke(null, new object[] {new[] {"claim-battle-pass"}, false}))
            throw new Exception("Battle pass selection widened the official task body");
    }

    static void TestInspectionScope(Type program, BindingFlags flags, string staging, string observedAt)
    {
        var json = new JavaScriptSerializer();
        Type bindingType = program.GetNestedType("Binding", BindingFlags.NonPublic);
        object binding = Activator.CreateInstance(bindingType, true);
        bindingType.GetField("WwInspectionSupported").SetValue(binding, true);
        bindingType.GetField("WwInspectionOnly").SetValue(binding, true);
        bindingType.GetField("AccountId").SetValue(binding, "fixture-owner");
        bindingType.GetField("AccountSnapshot").SetValue(binding, new Dictionary<string, object> {
            {"saved_account_label", "fixture-saved-account"}
        });
        string stem = "ww-recognition-1234567890abcdef1234567890abcdef";
        string image = Path.Combine(staging, stem + "_original.png");
        File.WriteAllBytes(image, Convert.FromBase64String("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="));
        MethodInfo hash = program.GetMethod("Hash", flags);
        string digest = (string)hash.Invoke(null, new object[] {image});
        var metadata = new Dictionary<string, object> {
            {"operation", "inspect-daily-progress"}, {"recognitionType", "ocr"},
            {"coordinateSpace", "full-frame"}, {"upstreamConfirmedAccount", "fixture-saved-account"},
            {"observedAt", observedAt}, {"screenshotSha256", digest}
        };
        string metadataPath = Path.Combine(staging, stem + ".json");
        File.WriteAllText(metadataPath, json.Serialize(metadata));
        File.WriteAllText(Path.Combine(staging, stem + "_original.json"), "{}");
        string before = File.ReadAllText(metadataPath);
        MethodInfo write = program.GetMethod("WriteWwInspectionScope", flags);
        var request = new Dictionary<string, object> {{"runId", "fixture-run"}, {"runAttemptId", "fixture-attempt"}};
        object[] inputs = {binding, request, "fixture-todo", "fixture-todo-attempt", image, observedAt};
        write.Invoke(null, inputs);
        var scope = json.DeserializeObject(File.ReadAllText(image + ".recognition.json")) as IDictionary<string, object>;
        if ((string)scope["sourceMetadataFile"] != stem + ".json" || File.ReadAllText(metadataPath) != before ||
            (string)hash.Invoke(null, new object[] {image}) != digest)
            throw new Exception("Inspection scope did not preserve the actual observer image/metadata mapping");
        metadata["upstreamConfirmedAccount"] = "foreign-account";
        File.WriteAllText(metadataPath, json.Serialize(metadata));
        try { write.Invoke(null, inputs); throw new Exception("Inspection scope accepted foreign-account metadata"); }
        catch (TargetInvocationException error) { if (!(error.InnerException is InvalidDataException)) throw; }
    }

    static void TestReplay(Type program, BindingFlags flags, string source, string staging,
                           string observedAt, string originalMetadata)
    {
        var json = new JavaScriptSerializer();
        string originalMetadataHash = (string)program.GetMethod("Hash", flags).Invoke(null,
            new object[] { Path.Combine(source, "recognition.json") });
        string imageDetail = "file=recognition.png; kind=game-ui-recognition-raw; capturedAt=" + observedAt;
        string metadataDetail = "file=recognition.json; kind=official-recognition-metadata; capturedAt=" + observedAt;
        string historicalDetail = "file=historical.png; kind=game-ui-step-before-raw; capturedAt=" + observedAt;
        byte[] pixels = Convert.FromBase64String("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=");
        File.WriteAllBytes(Path.Combine(source, "recognition.png"), pixels);
        File.WriteAllBytes(Path.Combine(source, "historical.png"), pixels);
        string observationFile = Path.Combine(source, "replay-stage.jsonl");
        var rows = new List<string>();
        foreach (string detail in new[] { imageDetail, metadataDetail, historicalDetail })
            rows.Add(json.Serialize(new Dictionary<string, object> {
                { "account", "fixture-saved-account" }, { "operation", "claim-daily-reward" },
                { "state", "step_artifact" }, { "detail", detail }
            }));
        rows.Add(json.Serialize(new Dictionary<string, object> {
            { "account", "different-account" }, { "operation", "claim-daily-reward" },
            { "state", "step_artifact" }, { "detail", imageDetail }
        }));
        File.WriteAllLines(observationFile, rows);
        Type bindingType = program.GetNestedType("Binding", BindingFlags.NonPublic);
        object binding = Activator.CreateInstance(bindingType, true);
        bindingType.GetField("AccountSnapshot").SetValue(binding,
            new Dictionary<string, object> { { "saved_account_label", "fixture-saved-account" } });
        var replayed = new List<Tuple<string, string, string>>();
        Action<string, string, string> observe = (operation, state, detail) => replayed.Add(Tuple.Create(operation, state, detail));
        DateTime before = DateTime.Now;
        program.GetMethod("ReplayWwOfficialObservations", flags).Invoke(null, new object[] { binding, observationFile, observe });
        DateTime after = DateTime.Now;
        if (replayed.Count != 3) throw new Exception("Replay crossed the official saved-account scope");
        var detailValue = program.GetMethod("DetailValue", flags);
        var imported = new Dictionary<string, List<Tuple<string, string, string>>>();
        for (int index = 0; index < replayed.Count; index++) {
            string detail = replayed[index].Item3;
            string expectedKind = index == 0 ? "game-ui-recognition-replay-raw" :
                index == 1 ? "official-recognition-metadata" : "game-ui-step-before-raw";
            if ((string)detailValue.Invoke(null, new object[] { detail, "kind" }) != expectedKind)
                throw new Exception("Replay disguised recognition time or renamed a historical action");
            if (index < 2 && (string)detailValue.Invoke(null, new object[] { detail, "originalObservedAt" }) != observedAt)
                throw new Exception("Replay lost the true original observation time");
            DateTime sealing = DateTime.Parse((string)detailValue.Invoke(null, new object[] { detail, "capturedAt" }));
            if (sealing < before || sealing > after) throw new Exception("Replay protocol time is not the current lease sealing time");
            program.GetMethod("ImportWwSidecarArtifact", flags).Invoke(null,
                new object[] { observationFile, replayed[index].Item1, replayed[index].Item2, detail, imported });
        }
        if (imported["claim-daily-reward"][0].Item2 != "game-ui-recognition-replay-raw" ||
            File.ReadAllText(Path.Combine(staging, "recognition.json")) != originalMetadata ||
            (string)program.GetMethod("Hash", flags).Invoke(null,
                new object[] { Path.Combine(staging, "recognition.json") }) != originalMetadataHash)
            throw new Exception("Replay importer changed the metadata bytes or concealed the replay kind");
        byte[] importedPixels = File.ReadAllBytes(Path.Combine(staging, "recognition.png"));
        if (Convert.ToBase64String(importedPixels) != Convert.ToBase64String(pixels))
            throw new Exception("Replay changed the original recognition image bytes");
        var live = new Dictionary<string, List<Tuple<string, string, string>>>();
        program.GetMethod("ImportWwSidecarArtifact", flags).Invoke(null,
            new object[] { observationFile, "claim-daily-reward", "step_artifact", imageDetail, live });
        if (live["claim-daily-reward"][0].Item2 != "game-ui-recognition-raw" || live["claim-daily-reward"][0].Item3 != observedAt)
            throw new Exception("Live recognition inherited replay kind or sealing time");
    }
}
